#!/usr/bin/env python3
"""Strict, standard-library verification for WikiLean authority contracts."""
from __future__ import annotations

import copy
import decimal
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections import defaultdict
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

try:
    from . import execution_environment as execution_environment_contract
except ImportError:  # Direct script/test imports place brain/tools on sys.path.
    import execution_environment as execution_environment_contract

MAX_SAFE_INTEGER = 9_007_199_254_740_991
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MEDIA_TYPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

SOURCE_SCHEMA_V1 = "wikilean.source-manifest/v1"
SOURCE_SCHEMA_V2 = "wikilean.source-manifest/v2"
PACK_SCHEMA_V1 = "wikilean.offline-pack/v1"
PACK_SCHEMA_V2 = "wikilean.offline-pack/v2"
REDUCER_INPUT_INVENTORY_SCHEMA_V2 = "wikilean.reducer-input-inventory/v2"
EXECUTION_ENVIRONMENT_SCHEMA = (
    execution_environment_contract.EXECUTION_ENVIRONMENT_SCHEMA
)
RELEASE_SCHEMA = "wikilean.release/v1"
BUILD_ATTESTATION_SCHEMA_V1 = "wikilean.build-attestation/v1"
BUILD_ATTESTATION_SCHEMA_V2 = "wikilean.build-attestation/v2"
VALIDATION_ATTESTATION_SCHEMA = "wikilean.validation-attestation/v1"
RELEASE_SELECTOR_SCHEMA = "wikilean.release-selector/v1"
RELEASE_PROFILE = "brain-current-v1"
# Compatibility aliases retained for every existing v1 caller and release.
SOURCE_SCHEMA = SOURCE_SCHEMA_V1
PACK_SCHEMA = PACK_SCHEMA_V1
BUILD_ATTESTATION_SCHEMA = BUILD_ATTESTATION_SCHEMA_V1
BRAIN_SQLITE_APPLICATION_ID = 0x574C424E  # ASCII "WLBN"

BRAIN_SQLITE_V2_TABLE_COLUMNS = {
    "snapshot": (
        "singleton",
        "schema_version",
        "build_state",
        "snapshot_id",
        "base_snapshot_id",
        "projection_id",
        "metadata_json",
    ),
    "artifacts": (
        "name",
        "generated_at",
        "row_count",
        "digest",
        "source_digest",
        "logical_digest",
        "raw_digest",
        "source_present",
        "metadata_json",
    ),
    "nodes": ("ordinal", "id", "type", "label", "payload_json"),
    "edges": (
        "stream",
        "ordinal",
        "src",
        "dst",
        "kind",
        "confidence",
        "provenance_source",
        "payload_json",
    ),
    "cells": ("ordinal", "id", "anchor", "label", "payload_json"),
    "organ_owners": ("organ_id", "owner_id", "organ_kind", "bare_decl"),
    "synapses": ("ordinal", "src", "dst", "weight", "payload_json"),
}
BRAIN_SQLITE_V2_INDEXES = {
    "nodes_type_label_idx": ("nodes", ("type", "label")),
    "edges_src_kind_idx": ("edges", ("src", "kind")),
    "edges_dst_kind_idx": ("edges", ("dst", "kind")),
    "edges_kind_stream_idx": ("edges", ("kind", "stream")),
    "cells_label_idx": ("cells", ("label",)),
    "organ_owners_owner_idx": ("organ_owners", ("owner_id",)),
    "organ_owners_bare_decl_idx": ("organ_owners", ("bare_decl",)),
    "synapses_src_idx": ("synapses", ("src",)),
    "synapses_dst_idx": ("synapses", ("dst",)),
}

SOURCE_DOMAIN_V1 = "wikilean.source-manifest.v1"
SOURCE_DOMAIN_V2 = "wikilean.source-manifest.v2"
SOURCE_SET_DOMAIN_V1 = "wikilean.source-set.v1"
SOURCE_SET_DOMAIN_V2 = "wikilean.source-set.v2"
PACK_DOMAIN_V1 = "wikilean.offline-pack.v1"
PACK_DOMAIN_V2 = "wikilean.offline-pack.v2"
REDUCER_INPUT_INVENTORY_DOMAIN_V2 = "wikilean.reducer-input-inventory.v2"
RELEASE_DOMAIN = "wikilean.release.v1"
BUILD_ATTESTATION_DOMAIN_V1 = "wikilean.build-attestation.v1"
BUILD_ATTESTATION_DOMAIN_V2 = "wikilean.build-attestation.v2"
VALIDATION_ATTESTATION_DOMAIN = "wikilean.validation-attestation.v1"
# Compatibility aliases retained for callers that intentionally construct v1.
SOURCE_DOMAIN = SOURCE_DOMAIN_V1
SOURCE_SET_DOMAIN = SOURCE_SET_DOMAIN_V1
PACK_DOMAIN = PACK_DOMAIN_V1
BUILD_ATTESTATION_DOMAIN = BUILD_ATTESTATION_DOMAIN_V1
LOGICAL_JSON_DOMAIN = "wikilean.logical-json.v1"
LOGICAL_JSONL_DOMAIN = "wikilean.logical-jsonl-rowset.v1"
COMPATIBILITY_SEMANTIC_STATE_DOMAIN = "wikilean.compatibility-semantic-state.v1"
LEGACY_DECLARED_INPUT_DOMAIN = "wikilean.legacy-declared-inputs.v1"

REQUIRED_RELEASE_PATHS = frozenset({
    "brain/data/nodes.jsonl",
    "brain/data/edges.jsonl",
    "brain/data/edges_links.jsonl",
    "brain/data/brain.sqlite3",
    "brain/data/cells.jsonl",
    "brain/data/synapses.jsonl",
    "brain/data/frontier.jsonl",
    "brain/data/frontier_graph.json",
    "brain/data/community_edges.jsonl",
    "catalog/data/source_registry.json",
    "site/assets/brain/sources.json",
    "site/assets/brain/xref_index.json",
    "site/assets/brain/cells/manifest.json",
    "site/assets/brain/cells/aliases.json",
    "site/assets/brain/cells/labels.json",
    "site/assets/brain/cells/supercells.json",
    "site/assets/brain/cells/explorer.json",
    "site/assets/brain/cells/frontier_graph.json",
    "site/out/brain.html",
})
STATIC_CELLS_PREFIX = "site/assets/brain/cells/"
COMPATIBILITY_SEMANTIC_PATHS = (
    "brain/data/nodes.jsonl",
    "brain/data/edges.jsonl",
    "brain/data/edges_links.jsonl",
    "brain/data/cells.jsonl",
    "brain/data/synapses.jsonl",
    "brain/data/frontier.jsonl",
    "brain/data/frontier_graph.json",
)


def _release_artifact_contract(path: str) -> tuple[str, str]:
    if path.endswith(".jsonl"):
        return "application/x-ndjson", "jsonl-rowset"
    if path.endswith(".json"):
        return "application/json", "json"
    if path.endswith(".html"):
        return "text/html", "opaque"
    if path.endswith(".sqlite3"):
        return "application/vnd.sqlite3", "opaque"
    _fail("$.artifacts", f"{RELEASE_PROFILE} does not support artifact path {path!r}")


class VerificationError(ValueError):
    """Raised when a contract or referenced local object is invalid."""


def _fail(location: str, message: str) -> None:
    raise VerificationError(f"{location}: {message}")


def _environment_contract_error(location: str, exc: Exception) -> VerificationError:
    message = str(exc)
    if message.startswith("$"):
        return VerificationError(f"{location}{message[1:]}")
    return VerificationError(f"{location}: {message}")


def execution_environment_identity(environment: dict[str, Any]) -> str:
    """Return the self-derived execution-environment/v1 identity."""
    try:
        return execution_environment_contract.execution_environment_identity(environment)
    except execution_environment_contract.ExecutionEnvironmentError as exc:
        raise _environment_contract_error("$", exc) from exc


def validate_execution_environment(
    environment: Any, *, location: str = "$"
) -> dict[str, Any]:
    """Validate an execution environment using the authority error type."""
    try:
        return execution_environment_contract.validate_execution_environment(environment)
    except execution_environment_contract.ExecutionEnvironmentError as exc:
        raise _environment_contract_error(location, exc) from exc


def _parse_integer(raw: str) -> int:
    if raw == "-0":
        raise VerificationError("JSON integer -0 is forbidden")
    value = int(raw)
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise VerificationError(f"JSON integer {raw} exceeds the portable range")
    return value


def _reject_float(raw: str) -> None:
    raise VerificationError(f"JSON number {raw!r} is not an integer")


def _reject_constant(raw: str) -> None:
    raise VerificationError(f"non-finite JSON number {raw!r} is forbidden")


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _check_unicode(value: Any, location: str = "$", *, require_nfc: bool = True) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            _fail(location, "strings must contain Unicode scalar values, not surrogates")
        if require_nfc and unicodedata.normalize("NFC", value) != value:
            _fail(location, "strings and object keys must already be Unicode NFC")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_unicode(item, f"{location}[{index}]", require_nfc=require_nfc)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_unicode(key, f"{location}.<key>", require_nfc=require_nfc)
            _check_unicode(item, f"{location}.{key}", require_nfc=require_nfc)


def _parse_decimal(raw: str) -> decimal.Decimal:
    try:
        value = decimal.Decimal(raw)
    except decimal.DecimalException as exc:
        raise VerificationError(f"JSON decimal {raw!r} is outside supported bounds") from exc
    if not value.is_finite():
        raise VerificationError(f"non-finite JSON number {raw!r} is forbidden")
    return value


def parse_artifact_json_bytes(data: bytes, *, location: str) -> Any:
    """Parse logical artifact JSON, preserving finite decimal numbers exactly."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{location}: not valid UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_int=_parse_integer,
            parse_float=_parse_decimal,
            parse_constant=_reject_constant,
        )
    except VerificationError as exc:
        raise VerificationError(f"{location}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"{location}: invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    # Generated artifacts may contain legacy non-NFC source text. Preserve its
    # exact code points, but still require valid Unicode scalar values.
    _check_unicode(value, require_nfc=False)
    return value


def parse_json_bytes(data: bytes, *, location: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{location}: not valid UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except VerificationError as exc:
        raise VerificationError(f"{location}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{location}: invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    _check_unicode(value)
    return value


def _decimal_json(value: Any) -> bytes:
    """Canonical artifact JSON with exact finite decimals and sorted keys."""
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, int):
        _parse_integer(str(value))
        return str(value).encode("ascii")
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            _fail("$", "non-finite decimal is forbidden")
        sign, raw_digits, exponent = value.as_tuple()
        digits = list(raw_digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not digits:
            return b"0"
        digit_text = "".join(str(digit) for digit in digits)
        point = len(digit_text) + exponent
        if point <= 0:
            rendered_length = (1 if sign else 0) + 2 + (-point) + len(digit_text)
            if rendered_length > 10_000:
                _fail("$", "decimal expansion exceeds 10000 canonical characters")
            text = "0." + "0" * (-point) + digit_text
        elif point >= len(digit_text):
            rendered_length = (1 if sign else 0) + point
            if rendered_length > 10_000:
                _fail("$", "decimal expansion exceeds 10000 canonical characters")
            text = digit_text + "0" * (point - len(digit_text))
        else:
            rendered_length = (1 if sign else 0) + len(digit_text) + 1
            if rendered_length > 10_000:
                _fail("$", "decimal expansion exceeds 10000 canonical characters")
            text = digit_text[:point] + "." + digit_text[point:]
        return (("-" if sign else "") + text).encode("ascii")
    if isinstance(value, list):
        return b"[" + b",".join(_decimal_json(item) for item in value) + b"]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                _fail("$", "object keys must be strings")
            parts.append(_decimal_json(key) + b":" + _decimal_json(value[key]))
        return b"{" + b",".join(parts) + b"}"
    _fail("$", f"unsupported logical artifact type {type(value).__name__}")


def canonical_artifact_json_bytes(value: Any) -> bytes:
    """Render parsed artifact JSON without losing finite decimal precision."""
    _check_unicode(value, require_nfc=False)
    return _decimal_json(value)


def _same_logical_json(left: Any, right: Any) -> bool:
    """Compare logical JSON recursively without materializing whole documents."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, decimal.Decimal)) or isinstance(
        right, (int, decimal.Decimal)
    ):
        if not isinstance(left, (int, decimal.Decimal)) or not isinstance(
            right, (int, decimal.Decimal)
        ):
            return False
        return decimal.Decimal(left) == decimal.Decimal(right)
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(
                _same_logical_json(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_same_logical_json(left[key], right[key]) for key in left)
        )
    return False


def canonical_json_bytes(value: Any) -> bytes:
    _validate_canonical_type(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_canonical_type(value: Any, location: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        _check_unicode(value, location)
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _fail(location, "integer exceeds the portable range")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical_type(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(location, "object keys must be strings")
            _check_unicode(key, f"{location}.<key>")
            _validate_canonical_type(item, f"{location}.{key}")
        return
    _fail(location, f"unsupported canonical JSON type {type(value).__name__}")


def domain_hash(domain: str, value: Any) -> str:
    if not domain.isascii():
        raise ValueError("hash domains must be ASCII")
    prefix = f"wikilean\0{domain}\0canonical-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + canonical_json_bytes(value)).hexdigest()


def load_canonical_json(path: Path) -> tuple[Any, bytes]:
    data = path.read_bytes()
    value = parse_json_bytes(data, location=str(path))
    expected = canonical_json_bytes(value)
    if data != expected:
        _fail(str(path), "document is not canonical-json-v1 bytes")
    return value, data


def _expect_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "expected an object")
    return value


def _expect_array(value: Any, location: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "expected an array")
    if nonempty and not value:
        _fail(location, "array must not be empty")
    return value


def _expect_string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(location, "expected a string")
    if nonempty and not value:
        _fail(location, "string must not be empty")
    return value


def _expect_int(value: Any, location: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(location, "expected an integer")
    if value < minimum or value > MAX_SAFE_INTEGER:
        _fail(location, f"integer must be between {minimum} and {MAX_SAFE_INTEGER}")
    return value


def _expect_pattern(value: Any, location: str, pattern: re.Pattern[str], description: str) -> str:
    text = _expect_string(value, location)
    if not pattern.fullmatch(text):
        _fail(location, f"expected {description}")
    return text


def _keys(obj: dict[str, Any], location: str, required: set[str], optional: set[str] = frozenset()) -> None:
    missing = sorted(required - obj.keys())
    unknown = sorted(obj.keys() - required - optional)
    if missing:
        _fail(location, f"missing required members: {', '.join(missing)}")
    if unknown:
        _fail(location, f"unknown members: {', '.join(unknown)}")


def _hash(value: Any, location: str) -> str:
    return _expect_pattern(value, location, HASH_RE, "sha256:<64 lowercase hex digits>")


def _digest(value: Any, location: str) -> str:
    return _expect_pattern(value, location, DIGEST_RE, "64 lowercase SHA-256 hex digits")


def validate_relative_path(value: Any, location: str) -> str:
    text = _expect_string(value, location)
    if "\\" in text or "\x00" in text:
        _fail(location, "path must use POSIX separators and contain no NUL")
    path = PurePosixPath(text)
    if path.is_absolute() or text != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        _fail(location, "path must be normalized, relative, and contain no '.' or '..' segment")
    return text


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


def validate_literal_relative_path(value: Any, location: str) -> str:
    text = validate_relative_path(value, location)
    if any(character in text for character in "*?[]{}"):
        _fail(location, "literal paths must not contain glob metacharacters")
    return text


def _relative_paths_overlap(left: str, right: str) -> bool:
    """Return whether either normalized relative path contains the other."""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


_PATH_TRIE_TERMINAL = object()


def _insert_relative_path(
    trie: dict[object, Any], path: str
) -> str | None:
    """Insert ``path`` or return an existing equal/ancestor/descendant path.

    Runtime input packs can contain hundreds of thousands of members, so path
    closure checks must scale with total path components rather than comparing
    every member with every earlier member.
    """
    node = trie
    for part in PurePosixPath(path).parts:
        if _PATH_TRIE_TERMINAL in node:
            return node[_PATH_TRIE_TERMINAL]
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        node = child
    if _PATH_TRIE_TERMINAL in node:
        return node[_PATH_TRIE_TERMINAL]
    if node:
        descendant = node
        while _PATH_TRIE_TERMINAL not in descendant:
            child_key = next(
                key for key in descendant if key is not _PATH_TRIE_TERMINAL
            )
            descendant = descendant[child_key]
        return descendant[_PATH_TRIE_TERMINAL]
    node[_PATH_TRIE_TERMINAL] = path
    return None


def _tool(value: Any, location: str) -> None:
    obj = _expect_object(value, location)
    _keys(obj, location, {"name", "version", "sha256"})
    _expect_string(obj["name"], f"{location}.name")
    _expect_string(obj["version"], f"{location}.version")
    _digest(obj["sha256"], f"{location}.sha256")


def _file_ref(value: Any, location: str, *, extra: set[str] = frozenset()) -> dict[str, Any]:
    obj = _expect_object(value, location)
    _keys(obj, location, {"path", "sha256", "bytes", "media_type"} | extra)
    validate_relative_path(obj["path"], f"{location}.path")
    _digest(obj["sha256"], f"{location}.sha256")
    _expect_int(obj["bytes"], f"{location}.bytes")
    _expect_pattern(obj["media_type"], f"{location}.media_type", MEDIA_TYPE_RE, "a media type")
    return obj


def _literal_file_ref(
    value: Any,
    location: str,
    *,
    extra: set[str] = frozenset(),
) -> dict[str, Any]:
    obj = _file_ref(value, location, extra=extra)
    validate_literal_relative_path(obj["path"], f"{location}.path")
    return obj


def _sorted_unique_strings(
    value: Any,
    location: str,
    *,
    nonempty: bool = False,
) -> list[str]:
    values = _expect_array(value, location, nonempty=nonempty)
    for index, item in enumerate(values):
        _expect_string(item, f"{location}[{index}]")
    if values != sorted(set(values)):
        _fail(location, "entries must be unique and sorted")
    return values


def source_manifest_identity(manifest: dict[str, Any]) -> str:
    domains = {
        SOURCE_SCHEMA_V1: SOURCE_DOMAIN_V1,
        SOURCE_SCHEMA_V2: SOURCE_DOMAIN_V2,
    }
    schema = manifest.get("schema")
    if schema not in domains:
        _fail("$.schema", f"unknown source-manifest schema/version {schema!r}")
    value = copy.deepcopy(manifest)
    value.pop("source_manifest_id", None)
    value.pop("audit", None)
    return domain_hash(domains[schema], value)


def source_set_root(manifest_ids: list[str]) -> str:
    return domain_hash(SOURCE_SET_DOMAIN_V1, sorted(manifest_ids))


def source_set_root_v2(
    inventory_id: str,
    manifest_ids: list[str],
    input_bindings: list[dict[str, Any]],
) -> str:
    """Bind v2 sources to their exact logical present/absent input mapping."""
    _hash(inventory_id, "$.inventory_id")
    normalized_manifests = sorted(manifest_ids)
    if len(normalized_manifests) != len(set(normalized_manifests)):
        _fail("$.source_manifests", "source manifest IDs must be unique")
    for index, manifest_id in enumerate(normalized_manifests):
        _hash(manifest_id, f"$.source_manifests[{index}]")

    normalized_bindings: list[dict[str, Any]] = []
    seen_inputs: set[str] = set()
    for index, value in enumerate(input_bindings):
        location = f"$.input_bindings[{index}]"
        binding = _expect_object(value, location)
        _keys(binding, location, {"input_id", "state", "members"})
        input_id = _expect_pattern(
            binding["input_id"], f"{location}.input_id", NAME_RE, "a lowercase input ID"
        )
        if input_id in seen_inputs:
            _fail(f"{location}.input_id", "duplicate input binding")
        seen_inputs.add(input_id)
        if binding["state"] not in {"present", "absent"}:
            _fail(f"{location}.state", "expected present or absent")
        members = _expect_array(binding["members"], f"{location}.members")
        normalized_members: list[dict[str, str]] = []
        member_paths: set[str] = set()
        for member_index, raw_member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            member = _expect_object(raw_member, member_location)
            _keys(member, member_location, {"path", "source_manifest_id", "object"})
            path = validate_literal_relative_path(
                member["path"], f"{member_location}.path"
            )
            if path in member_paths:
                _fail(f"{member_location}.path", "duplicate logical member path")
            member_paths.add(path)
            normalized_members.append({
                "path": path,
                "source_manifest_id": _hash(
                    member["source_manifest_id"],
                    f"{member_location}.source_manifest_id",
                ),
                "object": _expect_pattern(
                    member["object"],
                    f"{member_location}.object",
                    NAME_RE,
                    "a lowercase source object name",
                ),
            })
        normalized_members.sort(key=lambda item: item["path"])
        if binding["state"] == "absent" and normalized_members:
            _fail(f"{location}.members", "absent bindings must have no members")
        if binding["state"] == "present" and not normalized_members:
            _fail(f"{location}.members", "present bindings must have at least one member")
        normalized_bindings.append({
            "input_id": input_id,
            "state": binding["state"],
            "members": normalized_members,
        })
    normalized_bindings.sort(key=lambda item: item["input_id"])
    return domain_hash(
        SOURCE_SET_DOMAIN_V2,
        {
            "inventory_id": inventory_id,
            "source_manifests": normalized_manifests,
            "input_bindings": normalized_bindings,
        },
    )


def reducer_input_inventory_identity(inventory: dict[str, Any]) -> str:
    if inventory.get("schema") != REDUCER_INPUT_INVENTORY_SCHEMA_V2:
        _fail(
            "$.schema",
            f"unknown reducer-input inventory schema/version {inventory.get('schema')!r}",
        )
    value = copy.deepcopy(inventory)
    value.pop("inventory_id", None)
    return domain_hash(REDUCER_INPUT_INVENTORY_DOMAIN_V2, value)


def offline_pack_identity(pack: dict[str, Any]) -> str:
    domains = {
        PACK_SCHEMA_V1: PACK_DOMAIN_V1,
        PACK_SCHEMA_V2: PACK_DOMAIN_V2,
    }
    schema = pack.get("schema")
    if schema not in domains:
        _fail("$.schema", f"unknown offline-pack schema/version {schema!r}")
    value = copy.deepcopy(pack)
    value.pop("offline_pack_id", None)
    value.pop("audit", None)
    return domain_hash(domains[schema], value)


def release_identity(manifest: dict[str, Any]) -> str:
    value = copy.deepcopy(manifest)
    value.pop("release_id", None)
    value.pop("attestations", None)
    value.pop("created_at", None)
    return domain_hash(RELEASE_DOMAIN, value)


def compatibility_semantic_state_root(
    semantic_epoch: str,
    snapshot_id: str,
    logical_roots: dict[str, str],
) -> str:
    """Bind the current pre-changeset Brain state to its verified logical outputs."""
    _expect_pattern(semantic_epoch, "$.semantic_epoch", EPOCH_RE, "a semantic epoch")
    _expect_pattern(snapshot_id, "$.snapshot_id", DIGEST_RE, "64 lowercase SHA-256 hex digits")
    expected_paths = set(COMPATIBILITY_SEMANTIC_PATHS)
    if set(logical_roots) != expected_paths:
        missing = sorted(expected_paths - set(logical_roots))
        unknown = sorted(set(logical_roots) - expected_paths)
        _fail("$.logical_roots", f"expected compatibility paths (missing={missing}, unknown={unknown})")
    roots = []
    for path in COMPATIBILITY_SEMANTIC_PATHS:
        roots.append({"path": path, "logical_root": _hash(logical_roots[path], f"$.logical_roots.{path}")})
    return domain_hash(
        COMPATIBILITY_SEMANTIC_STATE_DOMAIN,
        {"semantic_epoch": semantic_epoch, "snapshot_id": snapshot_id, "logical_roots": roots},
    )


def legacy_declared_input_root(inventory_sha256: str, inputs: list[dict[str, Any]]) -> str:
    """Bind the compatibility source root to an inventory and exact input bytes."""
    _digest(inventory_sha256, "$.inventory_sha256")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(inputs):
        location = f"$.inputs[{index}]"
        item = _expect_object(value, location)
        _keys(item, location, {"declaration", "path", "present"}, {"sha256", "bytes"})
        declaration = _expect_string(item["declaration"], f"{location}.declaration")
        path = validate_relative_path(item["path"], f"{location}.path")
        if not isinstance(item["present"], bool):
            _fail(f"{location}.present", "expected a boolean")
        if item["present"]:
            _keys(item, location, {"declaration", "path", "present", "sha256", "bytes"})
            _digest(item["sha256"], f"{location}.sha256")
            _expect_int(item["bytes"], f"{location}.bytes")
        elif "sha256" in item or "bytes" in item:
            _fail(location, "absent inputs must not declare sha256 or bytes")
        key = (declaration, path)
        if key in seen:
            _fail(location, "duplicate declared input")
        seen.add(key)
        normalized.append(dict(item))
    normalized.sort(key=lambda item: (item["declaration"], item["path"]))
    return domain_hash(
        LEGACY_DECLARED_INPUT_DOMAIN,
        {"inventory_sha256": inventory_sha256, "inputs": normalized},
    )


def attestation_identity(attestation: dict[str, Any]) -> str:
    schema = attestation.get("schema")
    domains = {
        BUILD_ATTESTATION_SCHEMA_V1: BUILD_ATTESTATION_DOMAIN_V1,
        BUILD_ATTESTATION_SCHEMA_V2: BUILD_ATTESTATION_DOMAIN_V2,
        VALIDATION_ATTESTATION_SCHEMA: VALIDATION_ATTESTATION_DOMAIN,
    }
    if schema not in domains:
        _fail("$.schema", f"unknown attestation schema/version {schema!r}")
    value = copy.deepcopy(attestation)
    value.pop("attestation_id", None)
    value.pop("recorded_at", None)
    return domain_hash(domains[schema], value)


def validate_reducer_input_inventory(inventory: Any) -> dict[str, Any]:
    """Validate the v2 logical input and reducer-DAG inventory."""
    obj = _expect_object(inventory, "$")
    _keys(
        obj,
        "$",
        {
            "schema",
            "inventory_id",
            "boundary",
            "roots",
            "scope",
            "stages",
            "inputs",
            "forbidden_ambient",
        },
    )
    if obj["schema"] != REDUCER_INPUT_INVENTORY_SCHEMA_V2:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["inventory_id"], "$.inventory_id")
    if obj["boundary"] != "post-acquisition-fold":
        _fail("$.boundary", "expected 'post-acquisition-fold'")

    roots = _expect_array(obj["roots"], "$.roots", nonempty=True)
    root_ids: list[str] = []
    for index, value in enumerate(roots):
        location = f"$.roots[{index}]"
        root = _expect_object(value, location)
        _keys(root, location, {"id", "kind"})
        root_ids.append(
            _expect_pattern(root["id"], f"{location}.id", NAME_RE, "a lowercase root ID")
        )
        if root["kind"] not in {"repository", "external_tree", "external_file"}:
            _fail(f"{location}.kind", "unknown logical root kind")
    if root_ids != sorted(set(root_ids)):
        _fail("$.roots", "entries must have unique IDs and be sorted by id")
    root_id_set = set(root_ids)

    scope = _sorted_unique_strings(obj["scope"], "$.scope", nonempty=True)
    for index, path in enumerate(scope):
        validate_literal_relative_path(path, f"$.scope[{index}]")
    scope_set = set(scope)

    stages = _expect_array(obj["stages"], "$.stages", nonempty=True)
    prior_stages: set[str] = set()
    output_owners: dict[str, tuple[str, str]] = {}
    for index, value in enumerate(stages):
        location = f"$.stages[{index}]"
        stage = _expect_object(value, location)
        _keys(stage, location, {"id", "program", "argv", "needs", "outputs"})
        stage_id = _expect_pattern(
            stage["id"], f"{location}.id", NAME_RE, "a lowercase stage ID"
        )
        if stage_id in prior_stages:
            _fail(f"{location}.id", "duplicate stage ID")
        program = validate_literal_relative_path(stage["program"], f"{location}.program")
        if program not in scope_set:
            _fail(f"{location}.program", "stage program is absent from scope")
        argv = _expect_array(stage["argv"], f"{location}.argv")
        for argv_index, argument in enumerate(argv):
            _expect_string(argument, f"{location}.argv[{argv_index}]", nonempty=False)
        needs = _sorted_unique_strings(stage["needs"], f"{location}.needs")
        unknown_needs = sorted(set(needs) - prior_stages)
        if unknown_needs:
            _fail(
                f"{location}.needs",
                "dependencies must name earlier stages: " + ", ".join(unknown_needs),
            )
        outputs = _expect_array(stage["outputs"], f"{location}.outputs", nonempty=True)
        output_paths: list[str] = []
        for output_index, value in enumerate(outputs):
            output_location = f"{location}.outputs[{output_index}]"
            output = _expect_object(value, output_location)
            _keys(output, output_location, {"path", "kind"})
            output_path = validate_literal_relative_path(
                output["path"], f"{output_location}.path"
            )
            if output["kind"] not in {"file", "tree"}:
                _fail(f"{output_location}.kind", "expected file or tree")
            for owned_path, (owned_stage, owned_kind) in output_owners.items():
                if output_path == owned_path:
                    _fail(
                        f"{output_location}.path",
                        f"output path is already owned by stage {owned_stage!r}",
                    )
                if _relative_paths_overlap(output_path, owned_path):
                    _fail(
                        f"{output_location}.path",
                        "output ownership overlaps "
                        f"{owned_kind} {owned_path!r} owned by stage {owned_stage!r}",
                    )
            output_paths.append(output_path)
            output_owners[output_path] = (stage_id, output["kind"])
        if output_paths != sorted(set(output_paths)):
            _fail(
                f"{location}.outputs",
                "entries must have unique paths and be sorted by path",
            )
        prior_stages.add(stage_id)

    inputs = _expect_array(obj["inputs"], "$.inputs", nonempty=True)
    input_ids: list[str] = []
    for index, value in enumerate(inputs):
        location = f"$.inputs[{index}]"
        item = _expect_object(value, location)
        selector_keys = {key for key in ("path", "path_pattern") if key in item}
        required = {
            "id",
            "root",
            "cardinality",
            "requirement",
            "class",
            "consumers",
            "purpose",
        } | selector_keys
        _keys(item, location, required)
        if len(selector_keys) != 1:
            _fail(location, "must declare exactly one of path or path_pattern")
        input_ids.append(
            _expect_pattern(item["id"], f"{location}.id", NAME_RE, "a lowercase input ID")
        )
        root_id = _expect_pattern(
            item["root"], f"{location}.root", NAME_RE, "a lowercase root ID"
        )
        if root_id not in root_id_set:
            _fail(f"{location}.root", "unknown logical root")
        selector = next(iter(selector_keys))
        declared_path = validate_relative_path(item[selector], f"{location}.{selector}")
        if selector == "path":
            validate_literal_relative_path(declared_path, f"{location}.path")
        if selector == "path_pattern" and ("{" in declared_path or "}" in declared_path):
            _fail(f"{location}.path_pattern", "brace expansion is not supported")
        if item["cardinality"] not in {"one", "many"}:
            _fail(f"{location}.cardinality", "expected one or many")
        if item["cardinality"] == "one" and selector != "path":
            _fail(location, "cardinality 'one' requires path")
        if item["cardinality"] == "many" and selector != "path_pattern":
            _fail(location, "cardinality 'many' requires path_pattern")
        if item["requirement"] not in {"required", "optional"}:
            _fail(f"{location}.requirement", "expected required or optional")
        if item["class"] not in {"curated_git_input", "immutable_source_object"}:
            _fail(f"{location}.class", "unknown input class")
        consumers = _sorted_unique_strings(
            item["consumers"], f"{location}.consumers", nonempty=True
        )
        unknown_consumers = sorted(set(consumers) - scope_set)
        if unknown_consumers:
            _fail(
                f"{location}.consumers",
                "consumers are absent from scope: " + ", ".join(unknown_consumers),
            )
        _expect_string(item["purpose"], f"{location}.purpose")
    if input_ids != sorted(set(input_ids)):
        _fail("$.inputs", "entries must have unique IDs and be sorted by id")

    forbidden = _expect_array(obj["forbidden_ambient"], "$.forbidden_ambient")
    forbidden_names: list[str] = []
    for index, value in enumerate(forbidden):
        location = f"$.forbidden_ambient[{index}]"
        item = _expect_object(value, location)
        _keys(item, location, {"name", "consumers", "replacement"})
        forbidden_names.append(_expect_string(item["name"], f"{location}.name"))
        consumers = _sorted_unique_strings(
            item["consumers"], f"{location}.consumers", nonempty=True
        )
        if "*" in consumers and consumers != ["*"]:
            _fail(f"{location}.consumers", "'*' must be the only consumer")
        unknown_consumers = sorted(set(consumers) - scope_set - {"*"})
        if unknown_consumers:
            _fail(
                f"{location}.consumers",
                "consumers are absent from scope: " + ", ".join(unknown_consumers),
            )
        _expect_string(item["replacement"], f"{location}.replacement")
    if forbidden_names != sorted(set(forbidden_names)):
        _fail("$.forbidden_ambient", "entries must have unique names and be sorted by name")

    expected = reducer_input_inventory_identity(obj)
    if obj["inventory_id"] != expected:
        _fail("$.inventory_id", f"expected {expected}")
    return obj


def validate_source_manifest(manifest: Any) -> dict[str, Any]:
    if isinstance(manifest, dict) and manifest.get("schema") == SOURCE_SCHEMA_V2:
        return _validate_source_manifest_v2(manifest)
    obj = _expect_object(manifest, "$")
    _keys(
        obj,
        "$",
        {"schema", "source_manifest_id", "source", "pin", "objects", "license", "acquisition", "normalization"},
        {"previous_source_manifest_id", "review", "audit"},
    )
    if obj["schema"] != SOURCE_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["source_manifest_id"], "$.source_manifest_id")
    _expect_pattern(obj["source"], "$.source", NAME_RE, "a lowercase source name")

    pin = _expect_object(obj["pin"], "$.pin")
    _keys(pin, "$.pin", {"type", "value"})
    if pin["type"] not in {"git_commit", "content_sha256", "dataset_revision", "http_etag", "database_snapshot"}:
        _fail("$.pin.type", f"unknown pin type {pin['type']!r}")
    pin_value = _expect_string(pin["value"], "$.pin.value")
    if pin["type"] == "git_commit" and not GIT_COMMIT_RE.fullmatch(pin_value):
        _fail("$.pin.value", "git_commit pins must be full 40-character lowercase hashes")
    if pin["type"] == "content_sha256" and not DIGEST_RE.fullmatch(pin_value):
        _fail("$.pin.value", "content_sha256 pins must be 64 lowercase hex digits")

    objects = _expect_array(obj["objects"], "$.objects", nonempty=True)
    paths: set[str] = set()
    roles: set[str] = set()
    for index, item in enumerate(objects):
        location = f"$.objects[{index}]"
        ref = _file_ref(item, location, extra={"role"})
        if ref["role"] not in {"raw", "normalized"}:
            _fail(f"{location}.role", f"unknown source object role {ref['role']!r}")
        if ref["path"] in paths:
            _fail(f"{location}.path", "duplicate object path")
        paths.add(ref["path"])
        roles.add(ref["role"])
    if roles != {"raw", "normalized"}:
        _fail("$.objects", "must contain at least one raw and one normalized object")

    license_obj = _expect_object(obj["license"], "$.license")
    _keys(license_obj, "$.license", {"expression", "redistribution"}, {"attribution", "notice"})
    _expect_string(license_obj["expression"], "$.license.expression")
    if license_obj["redistribution"] not in {"allowed", "restricted", "link-only", "unknown"}:
        _fail("$.license.redistribution", "unknown redistribution policy")
    for key in ("attribution", "notice"):
        if key in license_obj and license_obj[key] is not None:
            _expect_string(license_obj[key], f"$.license.{key}", nonempty=False)

    _tool(obj["acquisition"], "$.acquisition")
    normalization = _expect_object(obj["normalization"], "$.normalization")
    _keys(normalization, "$.normalization", {"schema", "tool"})
    _expect_string(normalization["schema"], "$.normalization.schema")
    _tool(normalization["tool"], "$.normalization.tool")
    if "previous_source_manifest_id" in obj and obj["previous_source_manifest_id"] is not None:
        _hash(obj["previous_source_manifest_id"], "$.previous_source_manifest_id")
    if "review" in obj:
        review = _expect_object(obj["review"], "$.review")
        _keys(review, "$.review", {"summary", "expected_semantic_effects"})
        _expect_string(review["summary"], "$.review.summary", nonempty=False)
        effects = _expect_array(review["expected_semantic_effects"], "$.review.expected_semantic_effects")
        for index, effect in enumerate(effects):
            _expect_string(effect, f"$.review.expected_semantic_effects[{index}]", nonempty=False)
    if "audit" in obj:
        audit = _expect_object(obj["audit"], "$.audit")
        _keys(audit, "$.audit", set(), {"acquired_at", "upstream_uri"})
        for key, value in audit.items():
            _expect_string(value, f"$.audit.{key}")

    expected = source_manifest_identity(obj)
    if obj["source_manifest_id"] != expected:
        _fail("$.source_manifest_id", f"expected {expected}")
    return obj


def _validate_source_manifest_v2(manifest: Any) -> dict[str, Any]:
    obj = _expect_object(manifest, "$")
    _keys(
        obj,
        "$",
        {
            "schema",
            "source_manifest_id",
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
    if obj["schema"] != SOURCE_SCHEMA_V2:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["source_manifest_id"], "$.source_manifest_id")
    _expect_pattern(obj["source"], "$.source", NAME_RE, "a lowercase source name")
    if obj["source_kind"] not in {
        "acquired_dataset",
        "curated_git_tree",
        "sealed_snapshot",
    }:
        _fail("$.source_kind", "unknown source kind")

    pin = _expect_object(obj["pin"], "$.pin")
    _keys(pin, "$.pin", {"type", "value"}, {"tree"})
    if pin["type"] not in {
        "git_commit",
        "content_sha256",
        "dataset_revision",
        "http_etag",
        "database_snapshot",
    }:
        _fail("$.pin.type", f"unknown pin type {pin['type']!r}")
    pin_value = _expect_string(pin["value"], "$.pin.value")
    if len(pin_value) > 512:
        _fail("$.pin.value", "must contain at most 512 characters")
    if pin["type"] == "git_commit" and not GIT_COMMIT_RE.fullmatch(pin_value):
        _fail("$.pin.value", "git_commit pins must be full 40-character lowercase hashes")
    if pin["type"] == "content_sha256" and not DIGEST_RE.fullmatch(pin_value):
        _fail("$.pin.value", "content_sha256 pins must be 64 lowercase hex digits")
    if "tree" in pin:
        _expect_pattern(pin["tree"], "$.pin.tree", GIT_COMMIT_RE, "a full lowercase Git tree hash")
        if pin["type"] != "git_commit":
            _fail("$.pin.tree", "tree is permitted only with a git_commit pin")
    if obj["source_kind"] == "curated_git_tree":
        if pin["type"] != "git_commit" or "tree" not in pin:
            _fail("$.pin", "curated_git_tree sources require git_commit value and tree")

    objects = _expect_array(obj["objects"], "$.objects", nonempty=True)
    object_names: list[str] = []
    object_paths: set[str] = set()
    objects_by_name: dict[str, dict[str, Any]] = {}
    roles: set[str] = set()
    for index, item in enumerate(objects):
        location = f"$.objects[{index}]"
        ref = _file_ref(
            item,
            location,
            extra={"name", "roles", "redistribution"},
        )
        name = _expect_pattern(
            ref["name"], f"{location}.name", NAME_RE, "a lowercase source object name"
        )
        object_roles = _sorted_unique_strings(
            ref["roles"], f"{location}.roles", nonempty=True
        )
        unknown_roles = sorted(set(object_roles) - {"raw", "normalized", "receipt"})
        if unknown_roles:
            _fail(f"{location}.roles", "unknown source object roles: " + ", ".join(unknown_roles))
        if ref["redistribution"] not in {"allowed", "restricted", "link-only", "unknown"}:
            _fail(f"{location}.redistribution", "unknown redistribution policy")
        expected_path = f"objects/sha256/{ref['sha256']}"
        if ref["path"] != expected_path:
            _fail(f"{location}.path", f"expected content-addressed path {expected_path!r}")
        if name in objects_by_name or ref["path"] in object_paths:
            _fail(location, "duplicate source object name or path")
        object_names.append(name)
        object_paths.add(ref["path"])
        objects_by_name[name] = ref
        roles.update(object_roles)
    if object_names != sorted(object_names):
        _fail("$.objects", "entries must be sorted by name")
    if not {"raw", "normalized"}.issubset(roles):
        _fail("$.objects", "must contain at least one raw and one normalized object")

    license_obj = _expect_object(obj["license"], "$.license")
    _keys(license_obj, "$.license", {"expression", "redistribution"}, {"attribution", "notice"})
    _expect_string(license_obj["expression"], "$.license.expression")
    if license_obj["redistribution"] not in {"allowed", "restricted", "link-only", "unknown"}:
        _fail("$.license.redistribution", "unknown redistribution policy")
    for key in ("attribution", "notice"):
        if key in license_obj and license_obj[key] is not None:
            _expect_string(license_obj[key], f"$.license.{key}", nonempty=False)

    _tool(obj["acquisition"], "$.acquisition")
    normalization = _expect_object(obj["normalization"], "$.normalization")
    _keys(normalization, "$.normalization", {"schema", "tool", "inputs", "outputs"})
    _expect_string(normalization["schema"], "$.normalization.schema")
    _tool(normalization["tool"], "$.normalization.tool")
    normalization_inputs = _sorted_unique_strings(
        normalization["inputs"], "$.normalization.inputs", nonempty=True
    )
    normalization_outputs = _sorted_unique_strings(
        normalization["outputs"], "$.normalization.outputs", nonempty=True
    )
    raw_names = sorted(name for name, ref in objects_by_name.items() if "raw" in ref["roles"])
    normalized_names = sorted(
        name for name, ref in objects_by_name.items() if "normalized" in ref["roles"]
    )
    if normalization_inputs != raw_names:
        _fail("$.normalization.inputs", "must name every raw object exactly once")
    if normalization_outputs != normalized_names:
        _fail("$.normalization.outputs", "must name every normalized object exactly once")

    if "previous_source_manifest_id" in obj and obj["previous_source_manifest_id"] is not None:
        _hash(obj["previous_source_manifest_id"], "$.previous_source_manifest_id")
    if "review" in obj:
        review = _expect_object(obj["review"], "$.review")
        _keys(review, "$.review", {"summary", "expected_semantic_effects"})
        _expect_string(review["summary"], "$.review.summary", nonempty=False)
        effects = _expect_array(review["expected_semantic_effects"], "$.review.expected_semantic_effects")
        for index, effect in enumerate(effects):
            _expect_string(effect, f"$.review.expected_semantic_effects[{index}]", nonempty=False)
    if "audit" in obj:
        audit = _expect_object(obj["audit"], "$.audit")
        _keys(audit, "$.audit", set(), {"acquired_at", "upstream_uri"})
        for key, value in audit.items():
            _expect_string(value, f"$.audit.{key}")

    expected = source_manifest_identity(obj)
    if obj["source_manifest_id"] != expected:
        _fail("$.source_manifest_id", f"expected {expected}")
    return obj


def validate_offline_pack(pack: Any) -> dict[str, Any]:
    if isinstance(pack, dict) and pack.get("schema") == PACK_SCHEMA_V2:
        return _validate_offline_pack_v2(pack)
    obj = _expect_object(pack, "$")
    _keys(
        obj,
        "$",
        {"schema", "offline_pack_id", "source_set_root", "source_manifests", "objects", "reducer", "configuration", "schemas"},
        {"audit"},
    )
    if obj["schema"] != PACK_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["offline_pack_id"], "$.offline_pack_id")
    _hash(obj["source_set_root"], "$.source_set_root")

    manifests = _expect_array(obj["source_manifests"], "$.source_manifests", nonempty=True)
    manifest_ids: set[str] = set()
    manifest_paths: set[str] = set()
    for index, item in enumerate(manifests):
        location = f"$.source_manifests[{index}]"
        ref = _file_ref(item, location, extra={"source_manifest_id"})
        manifest_id = _hash(ref["source_manifest_id"], f"{location}.source_manifest_id")
        if manifest_id in manifest_ids or ref["path"] in manifest_paths:
            _fail(location, "duplicate source manifest ID or path")
        manifest_ids.add(manifest_id)
        manifest_paths.add(ref["path"])
    if [item["source_manifest_id"] for item in manifests] != sorted(manifest_ids):
        _fail("$.source_manifests", "entries must be sorted by source_manifest_id")

    objects = _expect_array(obj["objects"], "$.objects", nonempty=True)
    object_paths: set[str] = set()
    for index, item in enumerate(objects):
        ref = _file_ref(item, f"$.objects[{index}]")
        if ref["path"] in object_paths:
            _fail(f"$.objects[{index}].path", "duplicate object path")
        object_paths.add(ref["path"])
    if [item["path"] for item in objects] != sorted(object_paths):
        _fail("$.objects", "entries must be sorted by path")

    _file_ref(obj["reducer"], "$.reducer")
    _file_ref(obj["configuration"], "$.configuration")
    schemas = _expect_array(obj["schemas"], "$.schemas", nonempty=True)
    schema_paths: set[str] = set()
    for index, item in enumerate(schemas):
        ref = _file_ref(item, f"$.schemas[{index}]")
        if ref["path"] in schema_paths:
            _fail(f"$.schemas[{index}].path", "duplicate schema path")
        schema_paths.add(ref["path"])
    if [item["path"] for item in schemas] != sorted(schema_paths):
        _fail("$.schemas", "entries must be sorted by path")

    if "audit" in obj:
        audit = _expect_object(obj["audit"], "$.audit")
        _keys(audit, "$.audit", set(), {"created_at", "note"})
        for key, value in audit.items():
            _expect_string(value, f"$.audit.{key}", nonempty=False)

    expected = offline_pack_identity(obj)
    if obj["offline_pack_id"] != expected:
        _fail("$.offline_pack_id", f"expected {expected}")
    return obj


def _validate_offline_pack_v2(pack: Any) -> dict[str, Any]:
    obj = _expect_object(pack, "$")
    _keys(
        obj,
        "$",
        {
            "schema",
            "offline_pack_id",
            "source_set_root",
            "inventory",
            "source_manifests",
            "objects",
            "input_bindings",
            "reducer",
            "configuration",
            "environment",
            "schemas",
        },
        {"audit"},
    )
    if obj["schema"] != PACK_SCHEMA_V2:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["offline_pack_id"], "$.offline_pack_id")
    _hash(obj["source_set_root"], "$.source_set_root")

    inventory = _literal_file_ref(
        obj["inventory"], "$.inventory", extra={"inventory_id"}
    )
    _hash(inventory["inventory_id"], "$.inventory.inventory_id")
    if inventory["media_type"] != "application/json":
        _fail("$.inventory.media_type", "expected 'application/json'")

    manifests = _expect_array(obj["source_manifests"], "$.source_manifests", nonempty=True)
    manifest_ids: list[str] = []
    manifest_paths: set[str] = set()
    for index, item in enumerate(manifests):
        location = f"$.source_manifests[{index}]"
        ref = _literal_file_ref(item, location, extra={"source_manifest_id"})
        manifest_id = _hash(ref["source_manifest_id"], f"{location}.source_manifest_id")
        if ref["media_type"] != "application/json":
            _fail(f"{location}.media_type", "expected 'application/json'")
        if manifest_id in manifest_ids or ref["path"] in manifest_paths:
            _fail(location, "duplicate source manifest ID or path")
        manifest_ids.append(manifest_id)
        manifest_paths.add(ref["path"])
    if manifest_ids != sorted(manifest_ids):
        _fail("$.source_manifests", "entries must be sorted by source_manifest_id")

    objects = _expect_array(obj["objects"], "$.objects", nonempty=True)
    object_paths: list[str] = []
    for index, item in enumerate(objects):
        ref = _literal_file_ref(item, f"$.objects[{index}]")
        expected_path = f"objects/sha256/{ref['sha256']}"
        if ref["path"] != expected_path:
            _fail(f"$.objects[{index}].path", f"expected content-addressed path {expected_path!r}")
        object_paths.append(ref["path"])
    if object_paths != sorted(set(object_paths)):
        _fail("$.objects", "entries must have unique paths and be sorted by path")

    bindings = _expect_array(obj["input_bindings"], "$.input_bindings", nonempty=True)
    binding_ids: list[str] = []
    for index, value in enumerate(bindings):
        location = f"$.input_bindings[{index}]"
        binding = _expect_object(value, location)
        _keys(binding, location, {"input_id", "state", "members"})
        binding_ids.append(
            _expect_pattern(
                binding["input_id"], f"{location}.input_id", NAME_RE, "a lowercase input ID"
            )
        )
        if binding["state"] not in {"present", "absent"}:
            _fail(f"{location}.state", "expected present or absent")
        members = _expect_array(binding["members"], f"{location}.members")
        paths: list[str] = []
        for member_index, raw_member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            member = _expect_object(raw_member, member_location)
            _keys(member, member_location, {"path", "source_manifest_id", "object"})
            paths.append(
                validate_literal_relative_path(member["path"], f"{member_location}.path")
            )
            _hash(member["source_manifest_id"], f"{member_location}.source_manifest_id")
            _expect_pattern(
                member["object"],
                f"{member_location}.object",
                NAME_RE,
                "a lowercase source object name",
            )
        if paths != sorted(set(paths)):
            _fail(f"{location}.members", "entries must have unique paths and be sorted by path")
        if binding["state"] == "absent" and members:
            _fail(f"{location}.members", "absent bindings must have no members")
        if binding["state"] == "present" and not members:
            _fail(f"{location}.members", "present bindings must have at least one member")
    if binding_ids != sorted(set(binding_ids)):
        _fail("$.input_bindings", "entries must have unique IDs and be sorted by input_id")

    reducer = _expect_object(obj["reducer"], "$.reducer")
    _keys(reducer, "$.reducer", {"entrypoint", "files", "git_commit"})
    _expect_pattern(
        reducer["git_commit"],
        "$.reducer.git_commit",
        GIT_COMMIT_RE,
        "a full lowercase Git commit",
    )
    entrypoint = validate_literal_relative_path(
        reducer["entrypoint"], "$.reducer.entrypoint"
    )
    reducer_files = _expect_array(reducer["files"], "$.reducer.files", nonempty=True)
    reducer_logical_paths: list[str] = []
    for index, item in enumerate(reducer_files):
        location = f"$.reducer.files[{index}]"
        ref = _literal_file_ref(item, location, extra={"logical_path"})
        reducer_logical_paths.append(
            validate_literal_relative_path(
                ref["logical_path"], f"{location}.logical_path"
            )
        )
    if reducer_logical_paths != sorted(set(reducer_logical_paths)):
        _fail("$.reducer.files", "entries must have unique logical paths and be sorted by logical_path")
    reducer_path_trie: dict[object, Any] = {}
    for index, logical_path in enumerate(reducer_logical_paths):
        conflict = _insert_relative_path(reducer_path_trie, logical_path)
        if conflict is not None:
            _fail(
                f"$.reducer.files[{index}].logical_path",
                f"logical reducer path overlaps by ancestry with {conflict!r}",
            )
    if entrypoint not in reducer_logical_paths:
        _fail("$.reducer.entrypoint", "entrypoint is absent from reducer.files")

    configuration = _literal_file_ref(obj["configuration"], "$.configuration")
    if configuration["media_type"] != "application/json":
        _fail("$.configuration.media_type", "expected 'application/json'")
    refs = _expect_array(obj["schemas"], "$.schemas", nonempty=True)
    paths: list[str] = []
    for index, item in enumerate(refs):
        paths.append(_literal_file_ref(item, f"$.schemas[{index}]")["path"])
    if paths != sorted(set(paths)):
        _fail("$.schemas", "entries must have unique paths and be sorted by path")
    environment = _literal_file_ref(obj["environment"], "$.environment")
    if environment["media_type"] != "application/json":
        _fail("$.environment.media_type", "expected 'application/json'")

    if "audit" in obj:
        audit = _expect_object(obj["audit"], "$.audit")
        _keys(audit, "$.audit", set(), {"created_at", "note"})
        for key, value in audit.items():
            _expect_string(value, f"$.audit.{key}", nonempty=False)

    expected = offline_pack_identity(obj)
    if obj["offline_pack_id"] != expected:
        _fail("$.offline_pack_id", f"expected {expected}")
    return obj


@contextmanager
def open_regular_file(
    root: Path, relative_path: str, location: str
) -> Iterator[BinaryIO]:
    """Securely open a regular file beneath ``root`` without pre-reading it."""
    relative = validate_relative_path(relative_path, f"{location}.path")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(
            f"{location}.path: cannot resolve verification root: {exc}"
        ) from exc
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        try:
            descriptors.append(os.open(root, directory_flags))
            for part in PurePosixPath(relative).parts[:-1]:
                descriptors.append(
                    os.open(part, directory_flags, dir_fd=descriptors[-1])
                )
            file_descriptor = os.open(
                PurePosixPath(relative).name,
                file_flags,
                dir_fd=descriptors[-1],
            )
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                _fail(f"{location}.path", f"not a regular file: {relative}")
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise VerificationError(
                f"{location}.path: missing file or directory in {relative}"
            ) from exc
        except OSError as exc:
            raise VerificationError(
                f"{location}.path: cannot safely open {relative}: "
                f"{exc.strerror or exc}"
            ) from exc

        try:
            handle = os.fdopen(file_descriptor, "rb")
        except OSError as exc:
            raise VerificationError(
                f"{location}.path: cannot safely read {relative}: "
                f"{exc.strerror or exc}"
            ) from exc
        file_descriptor = None
        try:
            yield handle
        finally:
            handle.close()
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def open_verified_file(root: Path, ref: dict[str, Any], location: str) -> Iterator[BinaryIO]:
    """Open and hash-verify a regular file beneath root without symlinks."""
    relative = ref["path"]
    with open_regular_file(root, relative, location) as handle:
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        if size != ref["bytes"]:
            _fail(
                f"{location}.bytes",
                f"expected {ref['bytes']}, found {size} for {relative}",
            )
        actual_digest = digest.hexdigest()
        if actual_digest != ref["sha256"]:
            _fail(
                f"{location}.sha256",
                f"expected {ref['sha256']}, found {actual_digest} for {relative}",
            )
        handle.seek(0)
        yield handle


def digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_file_ref(root: Path, ref: dict[str, Any], location: str) -> bytes:
    with open_verified_file(root, ref, location) as handle:
        return handle.read()


def verify_file_ref_integrity(
    root: Path, ref: dict[str, Any], location: str
) -> None:
    """Verify a referenced file's size and digest without materializing its bytes."""
    with open_verified_file(root, ref, location):
        pass


def verify_source_manifest_files(manifest: dict[str, Any], root: Path) -> int:
    for index, ref in enumerate(manifest["objects"]):
        verify_file_ref_integrity(root, ref, f"$.objects[{index}]")
    return len(manifest["objects"])


def verify_offline_pack_files(
    pack: dict[str, Any], root: Path, *, manifest_path: Path | None = None
) -> dict[str, int]:
    if pack.get("schema") == PACK_SCHEMA_V2:
        return _verify_offline_pack_files_v2(pack, root, manifest_path=manifest_path)
    verified_paths: set[str] = set()
    object_refs = {ref["path"]: ref for ref in pack["objects"]}
    referenced_object_paths: set[str] = set()
    manifest_ids: list[str] = []
    source_object_count = 0

    for index, ref in enumerate(pack["source_manifests"]):
        location = f"$.source_manifests[{index}]"
        manifest_bytes = verify_file_ref(root, ref, location)
        source_manifest = parse_json_bytes(manifest_bytes, location=ref["path"])
        if manifest_bytes != canonical_json_bytes(source_manifest):
            _fail(location, "source manifest is not canonical-json-v1 bytes")
        validate_source_manifest(source_manifest)
        if source_manifest["source_manifest_id"] != ref["source_manifest_id"]:
            _fail(f"{location}.source_manifest_id", "does not match referenced manifest")
        manifest_ids.append(ref["source_manifest_id"])
        for source_index, source_ref in enumerate(source_manifest["objects"]):
            packed_ref = object_refs.get(source_ref["path"])
            if packed_ref is None:
                _fail(location, f"source object {source_ref['path']!r} is absent from pack.objects")
            for field in ("path", "sha256", "bytes", "media_type"):
                if packed_ref[field] != source_ref[field]:
                    _fail(location, f"source object {source_ref['path']!r} disagrees on {field}")
            referenced_object_paths.add(source_ref["path"])
            source_object_count += 1

    unreferenced = sorted(set(object_refs) - referenced_object_paths)
    if unreferenced:
        _fail("$.objects", f"unreferenced source objects: {', '.join(unreferenced)}")
    expected_source_root = source_set_root(manifest_ids)
    if pack["source_set_root"] != expected_source_root:
        _fail("$.source_set_root", f"expected {expected_source_root}")

    for index, ref in enumerate(pack["source_manifests"]):
        verified_paths.add(ref["path"])
    refs: list[tuple[str, dict[str, Any]]] = []
    refs.extend((f"$.objects[{i}]", ref) for i, ref in enumerate(pack["objects"]))
    refs.append(("$.reducer", pack["reducer"]))
    refs.append(("$.configuration", pack["configuration"]))
    refs.extend((f"$.schemas[{i}]", ref) for i, ref in enumerate(pack["schemas"]))
    for location, ref in refs:
        if ref["path"] in verified_paths:
            _fail(location, f"path {ref['path']!r} is listed in more than one pack section")
        verify_file_ref_integrity(root, ref, location)
        verified_paths.add(ref["path"])

    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            _fail("$", f"offline pack contains a symlink: {relative}")
        if path.is_file():
            actual_paths.add(relative)
        elif not path.is_dir():
            _fail("$", f"offline pack contains a non-regular entry: {relative}")
    if manifest_path is not None:
        try:
            manifest_relative = manifest_path.resolve(strict=True).relative_to(
                root.resolve(strict=True)
            ).as_posix()
        except ValueError as exc:
            _fail("$", "offline-pack manifest must reside beneath the verification root")
        actual_paths.discard(manifest_relative)
    undeclared = sorted(actual_paths - verified_paths)
    if undeclared:
        _fail("$", f"offline pack contains undeclared files: {', '.join(undeclared)}")

    return {
        "source_manifests": len(pack["source_manifests"]),
        "source_objects": source_object_count,
        "files": len(verified_paths),
    }


def _verify_offline_pack_files_v2(
    pack: dict[str, Any], root: Path, *, manifest_path: Path | None = None
) -> dict[str, int]:
    verified_paths: set[str] = set()

    inventory_ref = pack["inventory"]
    inventory_bytes = verify_file_ref(root, inventory_ref, "$.inventory")
    inventory = parse_json_bytes(inventory_bytes, location=inventory_ref["path"])
    if inventory_bytes != canonical_json_bytes(inventory):
        _fail("$.inventory", "reducer input inventory is not canonical-json-v1 bytes")
    validate_reducer_input_inventory(inventory)
    if inventory["inventory_id"] != inventory_ref["inventory_id"]:
        _fail("$.inventory.inventory_id", "does not match referenced inventory")
    verified_paths.add(inventory_ref["path"])

    environment_ref = pack["environment"]
    if environment_ref["path"] in verified_paths:
        _fail(
            "$.environment",
            f"path {environment_ref['path']!r} is listed in more than one pack section",
        )
    environment_bytes = verify_file_ref(root, environment_ref, "$.environment")
    environment = parse_json_bytes(
        environment_bytes, location=environment_ref["path"]
    )
    if environment_bytes != canonical_json_bytes(environment):
        _fail(
            "$.environment",
            "execution environment is not canonical-json-v1 bytes",
        )
    validate_execution_environment(environment, location="$.environment.document")
    if environment["runner"]["git_commit"] != pack["reducer"]["git_commit"]:
        _fail(
            "$.environment.document.runner.git_commit",
            "must equal $.reducer.git_commit",
        )
    verified_paths.add(environment_ref["path"])

    source_manifests: dict[str, dict[str, Any]] = {}
    source_objects: dict[tuple[str, str], dict[str, Any]] = {}
    referenced_object_paths: set[str] = set()
    for index, ref in enumerate(pack["source_manifests"]):
        location = f"$.source_manifests[{index}]"
        manifest_bytes = verify_file_ref(root, ref, location)
        source_manifest = parse_json_bytes(manifest_bytes, location=ref["path"])
        if manifest_bytes != canonical_json_bytes(source_manifest):
            _fail(location, "source manifest is not canonical-json-v1 bytes")
        validate_source_manifest(source_manifest)
        if source_manifest["schema"] != SOURCE_SCHEMA_V2:
            _fail(location, "offline-pack/v2 requires source-manifest/v2")
        manifest_id = ref["source_manifest_id"]
        if source_manifest["source_manifest_id"] != manifest_id:
            _fail(f"{location}.source_manifest_id", "does not match referenced manifest")
        if ref["path"] in verified_paths:
            _fail(location, f"path {ref['path']!r} is listed in more than one pack section")
        source_manifests[manifest_id] = source_manifest
        verified_paths.add(ref["path"])
        for object_ref in source_manifest["objects"]:
            key = (manifest_id, object_ref["name"])
            source_objects[key] = object_ref
            referenced_object_paths.add(object_ref["path"])

    packed_objects = {ref["path"]: ref for ref in pack["objects"]}
    missing_objects = sorted(referenced_object_paths - set(packed_objects))
    extra_objects = sorted(set(packed_objects) - referenced_object_paths)
    if missing_objects or extra_objects:
        _fail(
            "$.objects",
            f"source object closure mismatch (missing={missing_objects}, unreferenced={extra_objects})",
        )
    for (manifest_id, object_name), source_ref in source_objects.items():
        packed_ref = packed_objects[source_ref["path"]]
        for field in ("path", "sha256", "bytes", "media_type"):
            if packed_ref[field] != source_ref[field]:
                _fail(
                    "$.objects",
                    f"source object {manifest_id}/{object_name} disagrees on {field}",
                )

    inventory_inputs = {item["id"]: item for item in inventory["inputs"]}
    indexed_bindings = {
        item["input_id"]: (index, item)
        for index, item in enumerate(pack["input_bindings"])
    }
    bindings = {input_id: item for input_id, (_index, item) in indexed_bindings.items()}
    if set(bindings) != set(inventory_inputs):
        _fail(
            "$.input_bindings",
            "must bind every inventory input exactly once "
            f"(missing={sorted(set(inventory_inputs) - set(bindings))}, "
            f"unknown={sorted(set(bindings) - set(inventory_inputs))})",
        )
    logical_members: set[tuple[str, str]] = set()
    logical_path_tries: dict[str, dict[object, Any]] = defaultdict(dict)
    bound_manifest_ids: set[str] = set()
    for input_id, declaration in inventory_inputs.items():
        binding_index, binding = indexed_bindings[input_id]
        location = f"$.input_bindings[{binding_index}]"
        if declaration["requirement"] == "required" and binding["state"] != "present":
            _fail(location, "required inputs must be present")
        members = binding["members"]
        if declaration["cardinality"] == "one" and binding["state"] == "present" and len(members) != 1:
            _fail(f"{location}.members", "cardinality 'one' requires exactly one member")
        for member_index, member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            if declaration["cardinality"] == "one":
                matches = member["path"] == declaration["path"]
            else:
                matches = _matches_relative_pattern(
                    member["path"], declaration["path_pattern"]
                )
            if not matches:
                _fail(member_location, "logical member path does not match its inventory declaration")
            logical_key = (declaration["root"], member["path"])
            if logical_key in logical_members:
                _fail(member_location, "logical input path is bound more than once")
            conflict = _insert_relative_path(
                logical_path_tries[declaration["root"]], member["path"]
            )
            if conflict is not None:
                _fail(
                    member_location,
                    "logical input path overlaps by ancestry with "
                    f"{conflict!r} in root {declaration['root']!r}",
                )
            logical_members.add(logical_key)
            source_key = (member["source_manifest_id"], member["object"])
            source_ref = source_objects.get(source_key)
            if source_ref is None:
                _fail(member_location, "references an unknown source manifest object")
            if "normalized" not in source_ref["roles"]:
                _fail(member_location, "reducer inputs must bind normalized source objects")
            bound_manifest_ids.add(member["source_manifest_id"])
            if declaration["class"] == "curated_git_input":
                source_manifest = source_manifests[member["source_manifest_id"]]
                if source_manifest["source_kind"] != "curated_git_tree":
                    _fail(member_location, "curated Git inputs require a curated_git_tree source manifest")
    unused_manifests = sorted(set(source_manifests) - bound_manifest_ids)
    if unused_manifests:
        _fail(
            "$.source_manifests",
            "source manifests are not bound to any reducer input: " + ", ".join(unused_manifests),
        )

    expected_source_root = source_set_root_v2(
        inventory["inventory_id"],
        list(source_manifests),
        pack["input_bindings"],
    )
    if pack["source_set_root"] != expected_source_root:
        _fail("$.source_set_root", f"expected {expected_source_root}")

    reducer_paths = {ref["logical_path"] for ref in pack["reducer"]["files"]}
    if reducer_paths != set(inventory["scope"]):
        _fail(
            "$.reducer.files",
            "logical reducer closure must equal inventory scope "
            f"(missing={sorted(set(inventory['scope']) - reducer_paths)}, "
            f"unknown={sorted(reducer_paths - set(inventory['scope']))})",
        )

    refs: list[tuple[str, dict[str, Any]]] = []
    refs.extend((f"$.objects[{i}]", ref) for i, ref in enumerate(pack["objects"]))
    refs.extend((f"$.reducer.files[{i}]", ref) for i, ref in enumerate(pack["reducer"]["files"]))
    refs.append(("$.configuration", pack["configuration"]))
    refs.extend((f"$.schemas[{i}]", ref) for i, ref in enumerate(pack["schemas"]))
    for location, ref in refs:
        if ref["path"] in verified_paths:
            _fail(location, f"path {ref['path']!r} is listed in more than one pack section")
        verify_file_ref_integrity(root, ref, location)
        verified_paths.add(ref["path"])

    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            _fail("$", f"offline pack contains a symlink: {relative}")
        if path.is_file():
            actual_paths.add(relative)
        elif not path.is_dir():
            _fail("$", f"offline pack contains a non-regular entry: {relative}")
    if manifest_path is not None:
        try:
            manifest_relative = manifest_path.resolve(strict=True).relative_to(
                root.resolve(strict=True)
            ).as_posix()
        except ValueError as exc:
            _fail("$", "offline-pack manifest must reside beneath the verification root")
        actual_paths.discard(manifest_relative)
    undeclared = sorted(actual_paths - verified_paths)
    if undeclared:
        _fail("$", f"offline pack contains undeclared files: {', '.join(undeclared)}")

    return {
        "source_manifests": len(source_manifests),
        "source_objects": len(packed_objects),
        "input_bindings": len(bindings),
        "reducer_files": len(pack["reducer"]["files"]),
        "files": len(verified_paths),
    }


def _logical_json_bytes(data: bytes, location: str) -> str:
    value = parse_artifact_json_bytes(data, location=location)
    if isinstance(value, dict) and "_meta" in value:
        value = {key: item for key, item in value.items() if key != "_meta"}
    prefix = f"wikilean\0{LOGICAL_JSON_DOMAIN}\0canonical-artifact-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + _decimal_json(value)).hexdigest()


def logical_json_root(path: Path) -> str:
    return _logical_json_bytes(path.read_bytes(), str(path))


def _logical_jsonl_bytes(data: bytes, location: str) -> str:
    rows: list[bytes] = []
    for line_number, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        row = parse_artifact_json_bytes(raw, location=f"{location}:{line_number}")
        if isinstance(row, dict) and set(row) == {"_meta"}:
            continue
        rows.append(_decimal_json(row))
    rows.sort()
    canonical_rows = b"[" + b",".join(rows) + b"]"
    prefix = f"wikilean\0{LOGICAL_JSONL_DOMAIN}\0canonical-artifact-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + canonical_rows).hexdigest()


def _logical_jsonl_handle(handle: BinaryIO, location: str) -> str:
    """Compute the order-independent JSONL root with disk-backed sorting.

    Release artifacts are already hundreds of megabytes and are projected to
    grow into gigabytes.  Keeping every canonical row in a Python list makes
    verification scale with corpus size.  A temporary SQLite table preserves
    the exact bytewise ordering used by ``sorted(bytes)`` while bounding Python
    heap use to one input row plus a small insert batch.
    """
    connection = sqlite3.connect("")
    try:
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute("PRAGMA cache_size = -4096")
        connection.execute("CREATE TABLE rows (payload BLOB NOT NULL)")
        batch: list[tuple[Any, ...]] = []
        handle.seek(0)
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            row = parse_artifact_json_bytes(raw, location=f"{location}:{line_number}")
            if isinstance(row, dict) and set(row) == {"_meta"}:
                continue
            batch.append((sqlite3.Binary(_decimal_json(row)),))
            if len(batch) >= 1_000:
                connection.executemany("INSERT INTO rows VALUES (?)", batch)
                batch.clear()
        if batch:
            connection.executemany("INSERT INTO rows VALUES (?)", batch)

        digest = hashlib.sha256()
        digest.update(
            f"wikilean\0{LOGICAL_JSONL_DOMAIN}\0canonical-artifact-json-v1\0".encode(
                "ascii"
            )
        )
        digest.update(b"[")
        first = True
        for (payload,) in connection.execute("SELECT payload FROM rows ORDER BY payload"):
            if not first:
                digest.update(b",")
            digest.update(bytes(payload))
            first = False
        digest.update(b"]")
        return "sha256:" + digest.hexdigest()
    except sqlite3.Error as exc:
        raise VerificationError(f"{location}: cannot compute disk-backed JSONL root: {exc}") from exc
    finally:
        connection.close()


def logical_jsonl_root(path: Path) -> str:
    with path.open("rb") as handle:
        return _logical_jsonl_handle(handle, str(path))


def validate_release_manifest(manifest: Any) -> dict[str, Any]:
    obj = _expect_object(manifest, "$")
    _keys(
        obj,
        "$",
        {"schema", "profile", "release_id", "authority", "source_set_root", "semantic_epoch", "reducer", "artifacts", "attestations", "compatible_overlay_generation_ids"},
        {"created_at"},
    )
    if obj["schema"] != RELEASE_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    if obj["profile"] != RELEASE_PROFILE:
        _fail("$.profile", f"unknown release profile {obj['profile']!r}")
    _hash(obj["release_id"], "$.release_id")
    authority = _expect_object(obj["authority"], "$.authority")
    _keys(authority, "$.authority", {"git_commit", "semantic_state_root"}, {"through_changeset"})
    _expect_pattern(authority["git_commit"], "$.authority.git_commit", GIT_COMMIT_RE, "a full lowercase Git commit")
    _hash(authority["semantic_state_root"], "$.authority.semantic_state_root")
    if "through_changeset" in authority and authority["through_changeset"] is not None:
        _expect_string(authority["through_changeset"], "$.authority.through_changeset")
    _hash(obj["source_set_root"], "$.source_set_root")
    _expect_pattern(obj["semantic_epoch"], "$.semantic_epoch", EPOCH_RE, "a semantic epoch")

    reducer = _expect_object(obj["reducer"], "$.reducer")
    _keys(reducer, "$.reducer", {"schedule", "version", "git_commit", "configuration_sha256", "environment_sha256"})
    _expect_string(reducer["schedule"], "$.reducer.schedule")
    _expect_string(reducer["version"], "$.reducer.version")
    _expect_pattern(reducer["git_commit"], "$.reducer.git_commit", GIT_COMMIT_RE, "a full lowercase Git commit")
    _digest(reducer["configuration_sha256"], "$.reducer.configuration_sha256")
    _digest(reducer["environment_sha256"], "$.reducer.environment_sha256")

    artifacts = _expect_array(obj["artifacts"], "$.artifacts", nonempty=True)
    paths: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(artifacts):
        location = f"$.artifacts[{index}]"
        artifact = _expect_object(item, location)
        _keys(artifact, location, {"logical_name", "path", "media_type", "sha256", "bytes", "logical_format", "logical_root"}, {"uri"})
        name = _expect_string(artifact["logical_name"], f"{location}.logical_name")
        path = validate_relative_path(artifact["path"], f"{location}.path")
        _expect_pattern(artifact["media_type"], f"{location}.media_type", MEDIA_TYPE_RE, "a media type")
        _digest(artifact["sha256"], f"{location}.sha256")
        _expect_int(artifact["bytes"], f"{location}.bytes")
        if artifact["logical_format"] not in {"json", "jsonl-rowset", "opaque"}:
            _fail(f"{location}.logical_format", "unknown logical format")
        expected_media_type, expected_logical_format = _release_artifact_contract(path)
        if artifact["media_type"] != expected_media_type:
            _fail(
                f"{location}.media_type",
                f"expected {expected_media_type!r} for {path}",
            )
        if artifact["logical_format"] != expected_logical_format:
            _fail(
                f"{location}.logical_format",
                f"expected {expected_logical_format!r} for {path}",
            )
        if artifact["logical_format"] == "opaque":
            if artifact["logical_root"] is not None:
                _fail(f"{location}.logical_root", "opaque artifacts must use null")
        else:
            _hash(artifact["logical_root"], f"{location}.logical_root")
        if "uri" in artifact and artifact["uri"] is not None:
            _expect_string(artifact["uri"], f"{location}.uri")
        if path in paths or name in names:
            _fail(location, "duplicate artifact path or logical_name")
        paths.add(path)
        names.add(name)
    if [item["path"] for item in artifacts] != sorted(paths):
        _fail("$.artifacts", "entries must be sorted by path")

    missing = sorted(REQUIRED_RELEASE_PATHS - paths)
    if missing:
        _fail("$.artifacts", f"{RELEASE_PROFILE} is missing required paths: {', '.join(missing)}")
    if not any(path.startswith(STATIC_CELLS_PREFIX) and path not in REQUIRED_RELEASE_PATHS for path in paths):
        _fail("$.artifacts", f"{RELEASE_PROFILE} requires at least one generated cell or trace shard")

    attestations = _expect_array(obj["attestations"], "$.attestations", nonempty=True)
    attestation_paths: set[str] = set()
    kinds: set[str] = set()
    for index, item in enumerate(attestations):
        location = f"$.attestations[{index}]"
        ref = _expect_object(item, location)
        _keys(ref, location, {"kind", "path", "sha256", "bytes"})
        if ref["kind"] not in {"build", "validation"}:
            _fail(f"{location}.kind", "unknown attestation kind")
        validate_relative_path(ref["path"], f"{location}.path")
        _digest(ref["sha256"], f"{location}.sha256")
        _expect_int(ref["bytes"], f"{location}.bytes")
        if ref["path"] in attestation_paths:
            _fail(f"{location}.path", "duplicate attestation path")
        attestation_paths.add(ref["path"])
        kinds.add(ref["kind"])
    if kinds != {"build", "validation"}:
        _fail("$.attestations", "must contain at least one build and one validation attestation")
    if [item["path"] for item in attestations] != sorted(attestation_paths):
        _fail("$.attestations", "entries must be sorted by path")

    overlays = _expect_array(
        obj["compatible_overlay_generation_ids"],
        "$.compatible_overlay_generation_ids",
    )
    for index, overlay in enumerate(overlays):
        _expect_string(overlay, f"$.compatible_overlay_generation_ids[{index}]")
    if overlays != sorted(set(overlays)):
        _fail("$.compatible_overlay_generation_ids", "entries must be unique and sorted")
    if "created_at" in obj:
        _expect_string(obj["created_at"], "$.created_at")

    expected = release_identity(obj)
    if obj["release_id"] != expected:
        _fail("$.release_id", f"expected {expected}")
    return obj


def _validate_release_selection(value: Any, location: str) -> dict[str, Any]:
    selection = _expect_object(value, location)
    _keys(selection, location, {"release_id", "release", "manifest"})
    release_id = _hash(selection["release_id"], f"{location}.release_id")
    release_hex = _digest(selection["release"], f"{location}.release")
    if release_id != f"sha256:{release_hex}":
        _fail(location, "release must be the lowercase digest suffix of release_id")
    expected_manifest = f"/assets/brain/releases/{release_hex}/release.json"
    if selection["manifest"] != expected_manifest:
        _fail(f"{location}.manifest", f"expected {expected_manifest!r}")
    return selection


def validate_release_selector(selector: Any) -> dict[str, Any]:
    obj = _expect_object(selector, "$")
    previous_keys = {"previous_release_id", "previous_release", "previous_manifest"}
    _keys(
        obj,
        "$",
        {"schema", "release_id", "release", "manifest"},
        previous_keys | {"audited_at"},
    )
    if obj["schema"] != RELEASE_SELECTOR_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _validate_release_selection(
        {key: obj[key] for key in ("release_id", "release", "manifest")},
        "$",
    )
    present_previous = previous_keys.intersection(obj)
    if present_previous and present_previous != previous_keys:
        _fail("$", "previous release fields must be present together")
    if present_previous:
        previous = _validate_release_selection(
            {
                "release_id": obj["previous_release_id"],
                "release": obj["previous_release"],
                "manifest": obj["previous_manifest"],
            },
            "$.previous",
        )
        if previous["release_id"] == obj["release_id"]:
            _fail("$.previous_release_id", "previous release must differ from current release")
    if "audited_at" in obj:
        audited_at = _expect_string(obj["audited_at"], "$.audited_at")
        if not audited_at:
            _fail("$.audited_at", "must be non-empty")
    return obj


def validate_build_attestation(attestation: Any) -> dict[str, Any]:
    if isinstance(attestation, dict) and attestation.get("schema") == BUILD_ATTESTATION_SCHEMA_V2:
        return _validate_build_attestation_v2(attestation)
    obj = _expect_object(attestation, "$")
    _keys(
        obj,
        "$",
        {"schema", "attestation_id", "release_id", "builder", "input_roots", "output_root", "artifacts", "metrics"},
        {"recorded_at"},
    )
    if obj["schema"] != BUILD_ATTESTATION_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["attestation_id"], "$.attestation_id")
    _hash(obj["release_id"], "$.release_id")

    builder = _expect_object(obj["builder"], "$.builder")
    _keys(builder, "$.builder", {"name", "version", "git_commit", "configuration_sha256", "environment_sha256", "network"})
    _expect_string(builder["name"], "$.builder.name")
    _expect_string(builder["version"], "$.builder.version")
    _expect_pattern(builder["git_commit"], "$.builder.git_commit", GIT_COMMIT_RE, "a full lowercase Git commit")
    _digest(builder["configuration_sha256"], "$.builder.configuration_sha256")
    _digest(builder["environment_sha256"], "$.builder.environment_sha256")
    if builder["network"] != "disabled":
        _fail("$.builder.network", "build attestations require network='disabled'")

    roots = _expect_object(obj["input_roots"], "$.input_roots")
    _keys(roots, "$.input_roots", {"authority", "source_set"}, {"prior_state"})
    _hash(roots["authority"], "$.input_roots.authority")
    _hash(roots["source_set"], "$.input_roots.source_set")
    if "prior_state" in roots and roots["prior_state"] is not None:
        _hash(roots["prior_state"], "$.input_roots.prior_state")
    _hash(obj["output_root"], "$.output_root")

    artifacts = _expect_array(obj["artifacts"], "$.artifacts", nonempty=True)
    names: set[str] = set()
    for index, item in enumerate(artifacts):
        location = f"$.artifacts[{index}]"
        ref = _expect_object(item, location)
        _keys(ref, location, {"logical_name", "sha256", "bytes"}, {"logical_root"})
        name = _expect_pattern(ref["logical_name"], f"{location}.logical_name", NAME_RE, "a logical name")
        _digest(ref["sha256"], f"{location}.sha256")
        _expect_int(ref["bytes"], f"{location}.bytes")
        if "logical_root" in ref and ref["logical_root"] is not None:
            _hash(ref["logical_root"], f"{location}.logical_root")
        if name in names:
            _fail(f"{location}.logical_name", "duplicate logical name")
        names.add(name)
    if [item["logical_name"] for item in artifacts] != sorted(names):
        _fail("$.artifacts", "entries must be sorted by logical_name")

    metrics = _expect_object(obj["metrics"], "$.metrics")
    for key, value in metrics.items():
        _expect_string(key, "$.metrics.<key>")
        _expect_int(value, f"$.metrics.{key}")
    if "recorded_at" in obj:
        _expect_string(obj["recorded_at"], "$.recorded_at")
    expected = attestation_identity(obj)
    if obj["attestation_id"] != expected:
        _fail("$.attestation_id", f"expected {expected}")
    return obj


def _validate_build_attestation_v2(attestation: Any) -> dict[str, Any]:
    obj = _expect_object(attestation, "$")
    _keys(
        obj,
        "$",
        {
            "schema",
            "attestation_id",
            "release_id",
            "build_kind",
            "builder",
            "inputs",
            "output_root",
            "artifacts",
            "metrics",
        },
        {"recorded_at"},
    )
    if obj["schema"] != BUILD_ATTESTATION_SCHEMA_V2:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["attestation_id"], "$.attestation_id")
    _hash(obj["release_id"], "$.release_id")
    if obj["build_kind"] != "full-offline-replay":
        _fail("$.build_kind", "expected 'full-offline-replay'")

    builder = _expect_object(obj["builder"], "$.builder")
    _keys(
        builder,
        "$.builder",
        {
            "name",
            "version",
            "git_commit",
            "configuration_sha256",
            "environment_sha256",
            "network",
        },
    )
    _expect_string(builder["name"], "$.builder.name")
    _expect_string(builder["version"], "$.builder.version")
    _expect_pattern(
        builder["git_commit"],
        "$.builder.git_commit",
        GIT_COMMIT_RE,
        "a full lowercase Git commit",
    )
    _digest(builder["configuration_sha256"], "$.builder.configuration_sha256")
    _digest(builder["environment_sha256"], "$.builder.environment_sha256")
    if builder["network"] != "disabled":
        _fail("$.builder.network", "full offline replay requires network='disabled'")

    inputs = _expect_object(obj["inputs"], "$.inputs")
    _keys(
        inputs,
        "$.inputs",
        {
            "authority_root",
            "source_set_root",
            "offline_pack_id",
            "reducer_inventory_id",
            "prior_state_root",
        },
    )
    for key in (
        "authority_root",
        "source_set_root",
        "offline_pack_id",
        "reducer_inventory_id",
    ):
        _hash(inputs[key], f"$.inputs.{key}")
    if inputs["prior_state_root"] is not None:
        _hash(inputs["prior_state_root"], "$.inputs.prior_state_root")
    _hash(obj["output_root"], "$.output_root")

    artifacts = _expect_array(obj["artifacts"], "$.artifacts", nonempty=True)
    names: list[str] = []
    for index, item in enumerate(artifacts):
        location = f"$.artifacts[{index}]"
        ref = _expect_object(item, location)
        _keys(ref, location, {"logical_name", "sha256", "bytes"}, {"logical_root"})
        names.append(
            _expect_pattern(
                ref["logical_name"],
                f"{location}.logical_name",
                NAME_RE,
                "a logical name",
            )
        )
        _digest(ref["sha256"], f"{location}.sha256")
        _expect_int(ref["bytes"], f"{location}.bytes")
        if "logical_root" in ref and ref["logical_root"] is not None:
            _hash(ref["logical_root"], f"{location}.logical_root")
    if names != sorted(set(names)):
        _fail("$.artifacts", "entries must have unique logical names and be sorted")

    metrics = _expect_object(obj["metrics"], "$.metrics")
    for key, value in metrics.items():
        _expect_string(key, "$.metrics.<key>")
        _expect_int(value, f"$.metrics.{key}")
    if "recorded_at" in obj:
        _expect_string(obj["recorded_at"], "$.recorded_at")
    expected = attestation_identity(obj)
    if obj["attestation_id"] != expected:
        _fail("$.attestation_id", f"expected {expected}")
    return obj


def validate_validation_attestation(attestation: Any) -> dict[str, Any]:
    obj = _expect_object(attestation, "$")
    _keys(
        obj,
        "$",
        {"schema", "attestation_id", "release_id", "validator", "checks", "result"},
        {"recorded_at"},
    )
    if obj["schema"] != VALIDATION_ATTESTATION_SCHEMA:
        _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
    _hash(obj["attestation_id"], "$.attestation_id")
    _hash(obj["release_id"], "$.release_id")
    validator = _expect_object(obj["validator"], "$.validator")
    _keys(validator, "$.validator", {"name", "version", "git_commit", "environment_sha256", "network"})
    _expect_string(validator["name"], "$.validator.name")
    _expect_string(validator["version"], "$.validator.version")
    _expect_pattern(validator["git_commit"], "$.validator.git_commit", GIT_COMMIT_RE, "a full lowercase Git commit")
    _digest(validator["environment_sha256"], "$.validator.environment_sha256")
    if validator["network"] != "disabled":
        _fail("$.validator.network", "validation attestations require network='disabled'")

    checks = _expect_array(obj["checks"], "$.checks", nonempty=True)
    check_names: set[str] = set()
    for index, item in enumerate(checks):
        location = f"$.checks[{index}]"
        check = _expect_object(item, location)
        _keys(check, location, {"name", "status"}, {"details"})
        name = _expect_string(check["name"], f"{location}.name")
        if check["status"] not in {"pass", "fail"}:
            _fail(f"{location}.status", "expected pass or fail")
        if "details" in check:
            _expect_string(check["details"], f"{location}.details", nonempty=False)
        if name in check_names:
            _fail(f"{location}.name", "duplicate check name")
        check_names.add(name)
    if any(check["status"] != "pass" for check in checks) or obj["result"] != "pass":
        _fail("$.result", "an immutable release validation attestation must contain only passing checks")
    if "recorded_at" in obj:
        _expect_string(obj["recorded_at"], "$.recorded_at")
    expected = attestation_identity(obj)
    if obj["attestation_id"] != expected:
        _fail("$.attestation_id", f"expected {expected}")
    return obj


def _artifact_logical_root(data: bytes, logical_format: str, location: str) -> str | None:
    if logical_format == "json":
        return _logical_json_bytes(data, location)
    if logical_format == "jsonl-rowset":
        return _logical_jsonl_bytes(data, location)
    return None


def _artifact_logical_root_handle(
    handle: BinaryIO, logical_format: str, location: str
) -> str | None:
    if logical_format == "jsonl-rowset":
        return _logical_jsonl_handle(handle, location)
    if logical_format == "json":
        handle.seek(0)
        return _logical_json_bytes(handle.read(), location)
    return None


def _json_meta(data: bytes, location: str) -> dict[str, Any]:
    value = parse_artifact_json_bytes(data, location=location)
    if not isinstance(value, dict) or not isinstance(value.get("_meta"), dict):
        return {}
    return value["_meta"]


def _jsonl_meta(data: bytes, location: str) -> dict[str, Any]:
    for line_number, raw in enumerate(data.splitlines(), 1):
        if raw.strip():
            first = parse_artifact_json_bytes(raw, location=f"{location}:{line_number}")
            if isinstance(first, dict) and isinstance(first.get("_meta"), dict):
                return first["_meta"]
            return {}
    return {}


def _jsonl_meta_handle(handle: BinaryIO, location: str) -> dict[str, Any]:
    handle.seek(0)
    for line_number, raw in enumerate(handle, 1):
        if raw.strip():
            first = parse_artifact_json_bytes(raw, location=f"{location}:{line_number}")
            if isinstance(first, dict) and isinstance(first.get("_meta"), dict):
                return first["_meta"]
            return {}
    return {}


def _jsonl_rows(data: bytes, location: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        value = parse_artifact_json_bytes(raw, location=f"{location}:{line_number}")
        row = _expect_object(value, f"{location}:{line_number}")
        if set(row) == {"_meta"}:
            if meta is not None or rows:
                _fail(location, "metadata must be the first non-empty row")
            meta = _expect_object(row["_meta"], f"{location}:{line_number}._meta")
        else:
            rows.append(row)
    if meta is None:
        _fail(location, "missing metadata row")
    return meta, rows


def _trim_trace(trace: dict[str, Any]) -> dict[str, Any]:
    evidence = trace.get("evidence")
    if isinstance(evidence, dict) and len(evidence.get("witnesses") or []) > 1:
        return {**trace, "evidence": {**evidence, "witnesses": evidence["witnesses"][:1]}}
    return trace


def _pick_traces(traces: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        by_kind[trace.get("kind") or "?"].append(trace)
    result: list[dict[str, Any]] = []
    order = sorted(by_kind, key=lambda kind: (len(by_kind[kind]), kind))
    while len(result) < cap and any(by_kind.values()):
        progressed = False
        for kind in order:
            if by_kind[kind] and len(result) < cap:
                result.append(by_kind[kind].pop(0))
                progressed = True
        if not progressed:
            break
    return result


def _pick_synapses(synapses: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    if len(synapses) <= cap:
        return sorted(synapses, key=lambda item: (-item["w"], item["id"]))
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in synapses:
        for kind in entry["kinds"]:
            by_kind[kind].append(entry)
    for values in by_kind.values():
        values.sort(key=lambda item: (-item["w"], item["id"]))
    order = sorted(by_kind, key=lambda kind: (len(by_kind[kind]), kind))
    picked: dict[str, dict[str, Any]] = {}
    cursor = {kind: 0 for kind in order}
    while len(picked) < cap:
        progressed = False
        for kind in order:
            if len(picked) >= cap:
                break
            values = by_kind[kind]
            index = cursor[kind]
            while index < len(values) and values[index]["id"] in picked:
                index += 1
            cursor[kind] = index + 1
            if index < len(values):
                picked[values[index]["id"]] = values[index]
                progressed = True
        if not progressed:
            break
    return sorted(picked.values(), key=lambda item: (-item["w"], item["id"]))


def _organ_payload(organ: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    node = nodes.get(organ["id"]) or {}
    result = dict(organ)
    kind = organ["kind"]
    fields: tuple[str, ...] = ()
    if kind == "decl":
        fields = ("module", "decl_kind", "docstring", "code", "library", "renamed_to")
    elif kind == "page":
        fields = ("url", "kind_hint", "qid")
    elif kind == "statement":
        for field in ("arxiv_id", "ref", "license_open"):
            if node.get(field) is not None:
                result[field] = node[field]
    if kind in {"decl", "page"}:
        for field in fields:
            if node.get(field):
                result[field] = node[field]
    if kind == "concept":
        unit = node.get("unit") or {}
        if unit.get("description"):
            result["description"] = unit["description"]
        for field in ("slug", "article_annotations"):
            if node.get(field):
                result[field] = node[field]
        status = (node.get("display") or {}).get("status")
        if status:
            result["status"] = status
    elif kind == "page" and node.get("snippet"):
        snippet = node["snippet"]
        if len(snippet) > 400:
            snippet = snippet[:400].rsplit(" ", 1)[0] + "…"
        result["snippet"] = snippet
        result["snippet_license"] = node.get("snippet_license")
    return result


def _normalized_prefix(value: str, length: int, pad: str) -> str:
    # JavaScript indexes strings as UTF-16 code units. Mirror that client lookup
    # exactly so astral code points consume two padded positions here as well.
    units = value.encode("utf-16-le")
    result = ""
    for index in range(length):
        offset = index * 2
        if offset < len(units):
            code_unit = int.from_bytes(units[offset:offset + 2], "little")
            char = chr(code_unit).lower()
            result += char if ("a" <= char <= "z" or "0" <= char <= "9") else pad
        else:
            result += pad
    return result


def _routed_shard(
    value: str,
    keys: set[str],
    pad: str,
    lengths: tuple[int, ...] | None = None,
) -> str | None:
    """Route through the small set of prefix lengths, not every shard key."""
    ordered_lengths = lengths or tuple(sorted({len(key) for key in keys}, reverse=True))
    if not ordered_lengths:
        return None
    normalized = _normalized_prefix(value, ordered_lengths[0], pad)
    for length in ordered_lengths:
        candidate = normalized[:length]
        if candidate in keys:
            return candidate
    return None


def _validate_provenance_indexes(value: Any, table_size: int, location: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{location}.{key}"
            if key == "prov":
                if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < table_size:
                    _fail(child, f"must index a provenance table of size {table_size}")
            else:
                _validate_provenance_indexes(item, table_size, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_provenance_indexes(item, table_size, f"{location}[{index}]")


def _verify_current_static_closure(
    root: Path,
    artifacts: dict[str, dict[str, Any]],
    connection: sqlite3.Connection,
) -> None:
    artifact_paths = set(artifacts)

    def read_artifact(relative: str) -> bytes:
        ref = artifacts.get(relative)
        if ref is None:
            _fail("$.artifacts", f"release does not declare {relative}")
        return verify_file_ref(root, ref, f"static projection {relative}")

    manifest_relative = f"{STATIC_CELLS_PREFIX}manifest.json"
    manifest = parse_artifact_json_bytes(read_artifact(manifest_relative), location=manifest_relative)
    manifest_obj = _expect_object(manifest, manifest_relative)
    shards = _expect_object(manifest_obj.get("shards"), f"{manifest_relative}.shards")
    traces = _expect_object(manifest_obj.get("traces"), f"{manifest_relative}.traces")
    trace_files = _expect_object(traces.get("files"), f"{manifest_relative}.traces.files")
    scheme = _expect_object(manifest_obj.get("scheme"), f"{manifest_relative}.scheme")
    trace_scheme = _expect_object(traces.get("scheme"), f"{manifest_relative}.traces.scheme")
    shard_pad = _expect_string(scheme.get("pad"), f"{manifest_relative}.scheme.pad")
    trace_pad = _expect_string(trace_scheme.get("pad"), f"{manifest_relative}.traces.scheme.pad")
    expected = {f"{STATIC_CELLS_PREFIX}{key}.json" for key in shards}
    expected.update(f"{STATIC_CELLS_PREFIX}traces/{key}.json" for key in trace_files)
    shard_keys = set(shards)
    shard_lengths = tuple(sorted({len(key) for key in shard_keys}, reverse=True))
    trace_keys = set(trace_files)
    trace_lengths = tuple(sorted({len(key) for key in trace_keys}, reverse=True))
    missing = sorted(expected - artifact_paths)
    if missing:
        preview = ", ".join(missing[:5])
        suffix = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        _fail("$.artifacts", f"static cell manifest closure is missing {preview}{suffix}")
    dynamic_declared = {
        path for path in artifact_paths
        if path.startswith(STATIC_CELLS_PREFIX) and path not in REQUIRED_RELEASE_PATHS
    }
    stale = sorted(dynamic_declared - expected)
    if stale:
        preview = ", ".join(stale[:5])
        suffix = f" (and {len(stale) - 5} more)" if len(stale) > 5 else ""
        _fail("$.artifacts", f"static cell manifest closure contains stale files: {preview}{suffix}")

    def sqlite_metadata(name: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT metadata_json FROM artifacts WHERE name = ?", (name,)
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            _fail("$.artifacts", f"Brain SQLite metadata is missing for {name}")
        return _expect_object(
            parse_json_bytes(row[0].encode("utf-8"), location=f"SQLite artifacts.{name}"),
            f"SQLite artifacts.{name}",
        )

    def sqlite_payloads(
        query: str, parameters: tuple[Any, ...] = ()
    ) -> Iterator[dict[str, Any]]:
        for (payload,) in connection.execute(query, parameters):
            if not isinstance(payload, str):
                _fail("$.artifacts", "Brain SQLite payload_json must be text")
            yield _expect_object(
                parse_artifact_json_bytes(
                    payload.encode("utf-8"), location="Brain SQLite payload_json"
                ),
                "Brain SQLite payload_json",
            )

    cell_meta = sqlite_metadata("cells")
    cells: dict[str, dict[str, Any]] = {}
    cell_prov = cell_meta.get("prov", [])
    if not isinstance(cell_prov, list):
        _fail("brain/data/cells.jsonl._meta.prov", "must be an array")
    for index, row in enumerate(
        sqlite_payloads("SELECT payload_json FROM cells ORDER BY ordinal")
    ):
        cell_id = _expect_string(row.get("id"), "brain/data/cells.jsonl.id")
        if cell_id in cells:
            _fail("brain/data/cells.jsonl", "duplicate cell IDs")
        _validate_provenance_indexes(
            row, len(cell_prov), f"brain/data/cells.jsonl[{index}]"
        )
        cells[cell_id] = row
    if not _same_logical_json(manifest_obj.get("prov", []), cell_meta.get("prov", [])):
        _fail(manifest_relative, "prov does not match cells.jsonl metadata")

    expected_cell_ids = set(cells)
    ordered_cells = [cells[cell_id] for cell_id in sorted(cells)]
    cell_index = {cell["id"]: index for index, cell in enumerate(ordered_cells)}

    nodes = {
        _expect_string(row.get("id"), "brain/data/nodes.jsonl.id"): row
        for row in sqlite_payloads("SELECT payload_json FROM nodes ORDER BY ordinal")
    }

    parent: dict[str, str] = {}
    xrefs: dict[str, list[str]] = defaultdict(list)
    for row in sqlite_payloads(
        "SELECT payload_json FROM edges WHERE stream = 'main' ORDER BY ordinal"
    ):
        if (
            row.get("kind") == "contains"
            and isinstance(row.get("dst"), str)
            and row["dst"].startswith("path:")
        ):
            parent[row["dst"]] = row["src"]
        if row.get("kind") == "xref":
            xrefs[row["dst"]].append(row["src"])

    synapse_meta = sqlite_metadata("synapses")
    expected_trace_prov = synapse_meta.get("prov", [])
    if _same_logical_json(expected_trace_prov, cell_meta.get("prov", [])):
        if "prov" in traces:
            _fail(manifest_relative, "traces.prov must be omitted when provenance tables agree")
    elif not _same_logical_json(traces.get("prov"), expected_trace_prov):
        _fail(manifest_relative, "traces.prov does not match synapses.jsonl metadata")
    trace_prov = expected_trace_prov
    if not isinstance(trace_prov, list):
        _fail("brain/data/synapses.jsonl._meta.prov", "must be an array")

    frontier_meta, frontier_rows = _jsonl_rows(
        read_artifact("brain/data/frontier.jsonl"), "brain/data/frontier.jsonl"
    )

    def breadcrumb(owner: str | None) -> list[dict[str, str]]:
        chain: list[dict[str, str]] = []
        current = owner
        while current:
            node = nodes.get(current) or {}
            chain.insert(0, {"id": current, "label": node.get("label") or current.split("/")[-1]})
            current = parent.get(current)
        return chain

    expected_trace_values: dict[str, dict[str, Any]] = {}
    expected_explorer_edges: list[list[int]] = []
    for index, synapse in enumerate(
        sqlite_payloads("SELECT payload_json FROM synapses ORDER BY ordinal")
    ):
        _validate_provenance_indexes(
            synapse, len(trace_prov), f"brain/data/synapses.jsonl[{index}]"
        )
        src = _expect_string(synapse.get("src"), "brain/data/synapses.jsonl.src")
        dst = _expect_string(synapse.get("dst"), "brain/data/synapses.jsonl.dst")
        if src in cell_index and dst in cell_index:
            expected_explorer_edges.append(
                [cell_index[src], cell_index[dst], synapse["weight"]]
            )
        if src not in cells or dst not in cells:
            pair = f"{src}|{dst}"
            traces = [
                _trim_trace(trace)
                for trace in _pick_traces(synapse.get("traces") or [], 24)
            ]
            expected_trace_values[pair] = {
                "tt": len(synapse.get("traces") or []) + synapse.get("truncated", 0),
                "traces": traces,
            }
    expected_explorer_edges.sort(key=lambda value: (-value[2], value[0], value[1]))

    def synapses_for(owner: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        query = (
            "SELECT payload_json FROM ("
            "SELECT payload_json, ordinal FROM synapses INDEXED BY synapses_src_idx "
            "WHERE src = ? UNION ALL "
            "SELECT payload_json, ordinal FROM synapses INDEXED BY synapses_dst_idx "
            "WHERE dst = ? AND src <> ?"
            ") ORDER BY ordinal"
        )
        for synapse in sqlite_payloads(query, (owner, owner, owner)):
            src = _expect_string(synapse.get("src"), "brain/data/synapses.jsonl.src")
            dst = _expect_string(synapse.get("dst"), "brain/data/synapses.jsonl.dst")
            partner = dst if src == owner else src
            traces_for_card = [
                _trim_trace(trace)
                for trace in _pick_traces(synapse.get("traces") or [], 6)
            ]
            dropped = (
                len(synapse.get("traces") or [])
                - len(traces_for_card)
                + synapse.get("truncated", 0)
            )
            entry = {
                "id": partner,
                "w": synapse["weight"],
                "kinds": synapse["kinds"],
                "traces": traces_for_card,
            }
            if dropped:
                entry["tt"] = (
                    len(synapse.get("traces") or [])
                    + synapse.get("truncated", 0)
                )
            rows.append(entry)
        return rows

    served_cell_ids: set[str] = set()
    for key, declared_count in shards.items():
        _expect_int(declared_count, f"{manifest_relative}.shards.{key}")
        relative = f"{STATIC_CELLS_PREFIX}{key}.json"
        shard = _expect_object(parse_artifact_json_bytes(read_artifact(relative), location=relative), relative)
        if len(shard) != declared_count:
            _fail(relative, f"manifest declares {declared_count} entries, found {len(shard)}")
        for cell_id, entry_value in shard.items():
            routed = _routed_shard(cell_id, shard_keys, shard_pad, shard_lengths)
            if routed != key:
                _fail(relative, f"cell {cell_id!r} routes to shard {routed!r}, not {key!r}")
            if cell_id in served_cell_ids:
                _fail(relative, f"cell {cell_id!r} appears in more than one static shard")
            served_cell_ids.add(cell_id)
            cell = cells.get(cell_id)
            if cell is None:
                continue
            cell_synapses = synapses_for(cell_id)
            expected_entry: dict[str, Any] = {
                "cell": {field: value for field, value in cell.items() if field != "organs"},
                "organs": [_organ_payload(organ, nodes) for organ in cell.get("organs") or []],
                "syn": _pick_synapses(cell_synapses, 200),
                "counts": {
                    "syn": len(cell_synapses),
                    "organs": len(cell.get("organs") or []),
                },
            }
            if len(expected_entry["syn"]) < expected_entry["counts"]["syn"]:
                expected_entry["truncated"] = {
                    "syn": expected_entry["counts"]["syn"] - len(expected_entry["syn"])
                }
            supercells = cell.get("supercells") or []
            if supercells:
                expected_entry["breadcrumb"] = breadcrumb(
                    min(supercells, key=lambda value: (value.count("/"), value))
                )
            if not _same_logical_json(entry_value, expected_entry):
                _fail(relative, f"cell entry {cell_id!r} does not match JSONL projection")
    if served_cell_ids != expected_cell_ids:
        missing_cells = sorted(expected_cell_ids - served_cell_ids)
        unknown_cells = sorted(served_cell_ids - expected_cell_ids)
        _fail(
            "$.artifacts",
            "static cell shards do not exactly cover cells.jsonl "
            f"(missing={missing_cells[:5]}, unknown={unknown_cells[:5]})",
        )
    counts = _expect_object(manifest_obj.get("_meta"), f"{manifest_relative}._meta").get("counts")
    counts = _expect_object(counts, f"{manifest_relative}._meta.counts")
    if _expect_int(counts.get("cells"), f"{manifest_relative}._meta.counts.cells") != len(expected_cell_ids):
        _fail(f"{manifest_relative}._meta.counts.cells", "does not match cells.jsonl")
    if _expect_int(counts.get("shards"), f"{manifest_relative}._meta.counts.shards") != len(shards):
        _fail(f"{manifest_relative}._meta.counts.shards", "does not match manifest shards")

    labels_relative = f"{STATIC_CELLS_PREFIX}labels.json"
    labels = _expect_array(parse_artifact_json_bytes(read_artifact(labels_relative), location=labels_relative), labels_relative)
    expected_labels: list[dict[str, Any]] = []
    for cell_id, cell in sorted(cells.items()):
        ranked: dict[str, int] = {}
        for organ in cell.get("organs") or []:
            label = organ.get("label")
            if not label or label == cell.get("label"):
                continue
            rank = 0 if organ.get("kind") in {"concept", "decl", "article", "page"} else 1
            ranked[str(label)] = min(ranked.get(str(label), rank), rank)
        aliases = [alias for _, alias in sorted((rank, alias) for alias, rank in ranked.items())]
        row: dict[str, Any] = {"id": cell_id, "label": cell["label"]}
        if cell.get("f"):
            row["f"] = cell["f"]
        if aliases:
            row["aka"] = aliases[:16]
        supercells = cell.get("supercells") or []
        if supercells:
            row["p"] = min(supercells, key=lambda value: (value.count("/"), value))
        expected_labels.append(row)
    expected_labels.sort(key=lambda row: (-len(row.get("aka") or []), row["label"]))
    if not _same_logical_json(labels, expected_labels):
        _fail(labels_relative, "does not match the cells.jsonl search projection")

    aliases_relative = f"{STATIC_CELLS_PREFIX}aliases.json"
    aliases = _expect_object(parse_artifact_json_bytes(read_artifact(aliases_relative), location=aliases_relative), aliases_relative)
    expected_owners: dict[str, str] = {}
    expected_decls: dict[str, str] = {}
    expected_slugs: dict[str, str] = {}
    for cell_id, cell in cells.items():
        for organ in cell.get("organs") or []:
            organ_id = organ["id"]
            expected_owners[organ_id] = cell_id
            if organ.get("kind") == "decl":
                expected_decls[organ_id.split(":", 2)[2]] = cell_id
            elif organ.get("kind") == "article":
                expected_slugs[organ_id] = cell_id
            elif organ.get("kind") == "concept" and nodes.get(organ_id, {}).get("slug"):
                expected_slugs.setdefault(nodes[organ_id]["slug"], cell_id)
    for owner, organs in (cell_meta.get("supercell_organs") or {}).items():
        for organ in organs:
            organ_id = organ["id"]
            expected_owners.setdefault(organ_id, owner)
            if organ.get("kind") == "concept" and nodes.get(organ_id, {}).get("slug"):
                expected_slugs.setdefault(nodes[organ_id]["slug"], owner)
    for field, expected_map in (
        ("organs", expected_owners), ("decls", expected_decls), ("slugs", expected_slugs)
    ):
        if aliases.get(field) != {key: expected_map[key] for key in sorted(expected_map)}:
            _fail(aliases_relative, f"{field} does not match the JSONL projection")

    frontier_source = read_artifact("brain/data/frontier_graph.json")
    frontier_copy = read_artifact(f"{STATIC_CELLS_PREFIX}frontier_graph.json")
    if frontier_copy != frontier_source:
        _fail("$.artifacts", "static frontier_graph.json is not the verbatim Brain data copy")

    explorer_relative = f"{STATIC_CELLS_PREFIX}explorer.json"
    explorer = _expect_object(
        parse_artifact_json_bytes(read_artifact(explorer_relative), location=explorer_relative),
        explorer_relative,
    )
    expected_explorer_nodes = [
        {
            "id": cell["id"],
            "label": cell["label"],
            "xy": cell["xy"],
            **({"f": cell["f"]} if cell.get("f") else {}),
            **({"p": min(cell["supercells"], key=lambda value: (value.count("/"), value))}
               if cell.get("supercells") else {}),
        }
        for cell in ordered_cells
    ]
    if not _same_logical_json(explorer.get("nodes"), expected_explorer_nodes) \
            or not _same_logical_json(explorer.get("edges"), expected_explorer_edges):
        _fail(explorer_relative, "does not match the complete cells/synapses projection")

    supercells_relative = f"{STATIC_CELLS_PREFIX}supercells.json"
    supercells_doc = _expect_object(
        parse_artifact_json_bytes(read_artifact(supercells_relative), location=supercells_relative),
        supercells_relative,
    )
    expected_members: dict[str, list[str]] = defaultdict(list)
    expected_children: dict[str, list[str]] = defaultdict(list)
    facets: dict[str, int] = defaultdict(int)
    subtree_cells: dict[str, int] = defaultdict(int)
    for cell_id, cell in cells.items():
        for owner in cell.get("supercells") or []:
            expected_members[owner].append(cell_id)
        bits = cell.get("f", 0)
        seen: set[str] = set()
        for owner in cell.get("supercells") or []:
            current: str | None = owner
            while current is not None and current not in seen:
                seen.add(current)
                facets[current] |= bits
                current = parent.get(current)
        for owner in seen:
            subtree_cells[owner] += 1
    for child, owner in parent.items():
        expected_children[owner].append(child)
    supercell_organs = cell_meta.get("supercell_organs") or {}
    expected_supercells: dict[str, dict[str, Any]] = {}
    path_ids = set(expected_members) | set(expected_children) | set(parent)
    for path_id in sorted(path_ids):
        node = nodes.get(path_id) or {}
        row: dict[str, Any] = {"label": node.get("label") or path_id.split("/")[-1]}
        if facets.get(path_id):
            row["fa"] = facets[path_id]
        if parent.get(path_id):
            row["parent"] = parent[path_id]
        if expected_children.get(path_id):
            row["children"] = sorted(expected_children[path_id])
        if expected_members.get(path_id):
            row["cells"] = sorted(expected_members[path_id])
        if supercell_organs.get(path_id):
            row["organs"] = supercell_organs[path_id]
        synapses = synapses_for(path_id)
        if synapses:
            kept = _pick_synapses(synapses, 200)
            row["syn"] = [
                {key: value for key, value in synapse.items() if key != "traces"}
                for synapse in kept
            ]
            row["counts"] = {"syn": len(synapses)}
            if len(kept) < len(synapses):
                row["truncated"] = {"syn": len(synapses) - len(kept)}
        expected_supercells[path_id] = row
    for frontier in frontier_rows:
        frontier_id = frontier["id"]
        row = {
            "label": frontier.get("label") or frontier_id.split(":", 1)[1],
            "frontier": True,
            "cells": frontier["cells"],
        }
        area_facets = 0
        for cell_id in frontier["cells"]:
            area_facets |= cells[cell_id].get("f", 0)
        if area_facets:
            row["fa"] = area_facets
        for source_field, output_field in (
            ("near", "near"), ("mean_stateability", "stateability"), ("top", "top"),
        ):
            if frontier.get(source_field) is not None and frontier.get(source_field) != []:
                row[output_field] = frontier[source_field]
        for field in ("prox", "suitability"):
            value = frontier.get(field)
            if value and all(
                isinstance(items, list) and len(items) == len(frontier["cells"])
                for items in value.values()
            ):
                row[field] = value
        expected_supercells[frontier_id] = row
    if not _same_logical_json(supercells_doc.get("supercells"), expected_supercells):
        _fail(supercells_relative, "does not match the complete cell/frontier projection")
    expected_roots = sorted(path_id for path_id in expected_supercells if path_id not in parent)
    if not _same_logical_json(supercells_doc.get("roots"), expected_roots):
        _fail(supercells_relative, "roots do not match the containment projection")
    expected_manifest_roots: list[dict[str, Any]] = []
    for path_id in expected_roots:
        row = expected_supercells[path_id]
        if row.get("frontier"):
            root_row: dict[str, Any] = {
                "id": path_id,
                "frontier": True,
                "label": row["label"],
                "cells": len(row.get("cells") or []),
            }
            if row.get("fa"):
                root_row["fa"] = row["fa"]
        else:
            node = nodes.get(path_id) or {}
            root_row = {
                "id": path_id,
                "label": node.get("label") or path_id[5:],
            }
            for field in ("library_kind", "n_decls", "n_files"):
                if node.get(field) is not None:
                    root_row[field] = node[field]
            if subtree_cells.get(path_id):
                root_row["cells"] = subtree_cells[path_id]
            if facets.get(path_id):
                root_row["fa"] = facets[path_id]
        expected_manifest_roots.append(root_row)
    if not _same_logical_json(manifest_obj.get("roots"), expected_manifest_roots):
        _fail(manifest_relative, "roots do not match the complete containment/frontier projection")
    expected_counts = {
        "supercells": len(expected_supercells),
        "with_cells": sum(1 for row in expected_supercells.values() if row.get("cells")),
        "synapse_rows": sum(len(row.get("syn") or []) for row in expected_supercells.values()),
        "frontier_areas": len(frontier_rows),
        "frontier_cells": sum(len(row["cells"]) for row in frontier_rows),
        "frontier_homeless": (frontier_meta.get("counts") or {}).get(
            "homeless", sum(len(row["cells"]) for row in frontier_rows)
        ),
        "frontier_unclaimed": max(
            (frontier_meta.get("counts") or {}).get(
                "homeless", sum(len(row["cells"]) for row in frontier_rows)
            ) - len({cell_id for row in frontier_rows for cell_id in row["cells"]}),
            0,
        ),
    }
    actual_counts = _expect_object(
        _expect_object(supercells_doc.get("_meta"), f"{supercells_relative}._meta").get("counts"),
        f"{supercells_relative}._meta.counts",
    )
    if not _same_logical_json(actual_counts, expected_counts):
        _fail(supercells_relative, "metadata counts do not match the complete projection")

    source_registry = _expect_object(
        parse_artifact_json_bytes(
            read_artifact("catalog/data/source_registry.json"),
            location="catalog/data/source_registry.json",
        ),
        "catalog/data/source_registry.json",
    )
    expected_sources: list[dict[str, Any]] = []
    source_fields = (
        "name", "homepage", "layer", "kind", "our_provenance",
        "target_license", "wikidata_property", "note",
    )
    def add_source(key: str, value: dict[str, Any], group: str) -> None:
        expected_sources.append({field: value.get(field, "") for field in source_fields} | {
            "key": key, "group": group,
        })
    spine = _expect_object(source_registry.get("spine"), "source_registry.spine")
    add_source(_expect_string(spine.get("key"), "source_registry.spine.key"), spine, "spine")
    for group in (
        "node_sources", "edge_sources", "crossref_sources", "literature_sources",
        "frontier_sources", "brain_sources",
    ):
        for key, value in _expect_object(source_registry.get(group, {}), f"source_registry.{group}").items():
            add_source(key, _expect_object(value, f"source_registry.{group}.{key}"), group)
    sources_doc = parse_artifact_json_bytes(
        read_artifact("site/assets/brain/sources.json"), location="site/assets/brain/sources.json"
    )
    expected_sources_doc = {
        "layers": source_registry["layers"],
        "our_data_license": source_registry["our_data_license"],
        "sources": expected_sources,
    }
    if not _same_logical_json(sources_doc, expected_sources_doc):
        _fail("site/assets/brain/sources.json", "does not match source_registry.json")

    for line_number, raw in enumerate(read_artifact("brain/data/community_edges.jsonl").splitlines(), 1):
        if not raw.strip():
            continue
        row = _expect_object(
            parse_artifact_json_bytes(raw, location=f"brain/data/community_edges.jsonl:{line_number}"),
            f"brain/data/community_edges.jsonl:{line_number}",
        )
        if row.get("kind") == "xref":
            xrefs[row["dst"]].append(row["src"])
    xref_doc = parse_artifact_json_bytes(
        read_artifact("site/assets/brain/xref_index.json"), location="site/assets/brain/xref_index.json"
    )
    if not _same_logical_json(xref_doc, dict(xrefs)):
        _fail("site/assets/brain/xref_index.json", "does not match edge inputs")

    served_trace_values: dict[str, dict[str, Any]] = {}
    for key, declared_count in trace_files.items():
        _expect_int(declared_count, f"{manifest_relative}.traces.files.{key}")
        relative = f"{STATIC_CELLS_PREFIX}traces/{key}.json"
        shard = _expect_object(parse_artifact_json_bytes(read_artifact(relative), location=relative), relative)
        if len(shard) != declared_count:
            _fail(relative, f"manifest declares {declared_count} entries, found {len(shard)}")
        for pair, trace_value in shard.items():
            routed = _routed_shard(pair, trace_keys, trace_pad, trace_lengths)
            if routed != key:
                _fail(relative, f"trace pair {pair!r} routes to shard {routed!r}, not {key!r}")
            if pair in served_trace_values:
                _fail(relative, f"trace pair {pair!r} appears in more than one shard")
            served_trace_values[pair] = trace_value
    if not _same_logical_json(served_trace_values, expected_trace_values):
        _fail("$.artifacts", "trace sidecars do not match supercell synapse projections")

    cells_root = root.resolve(strict=True) / "site/assets/brain/cells"
    actual = {
        f"{STATIC_CELLS_PREFIX}{path.relative_to(cells_root).as_posix()}"
        for path in cells_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    declared = {path for path in artifact_paths if path.startswith(STATIC_CELLS_PREFIX)}
    unlisted = sorted(actual - declared)
    if unlisted:
        preview = ", ".join(unlisted[:5])
        suffix = f" (and {len(unlisted) - 5} more)" if len(unlisted) > 5 else ""
        _fail("$.artifacts", f"static cell directory contains unlisted files: {preview}{suffix}")


def _sqlite_payload_root(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> str:
    table = "release_logical_rows"
    connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
    connection.execute(f"CREATE TEMP TABLE {table} (payload BLOB NOT NULL)")
    try:
        batch: list[tuple[Any, ...]] = []
        for (payload,) in connection.execute(query, parameters):
            if not isinstance(payload, str):
                _fail("$.artifacts", "SQLite payload_json must be text")
            value = parse_artifact_json_bytes(
                payload.encode("utf-8"), location="SQLite payload_json"
            )
            batch.append((sqlite3.Binary(_decimal_json(value)),))
            if len(batch) >= 1_000:
                connection.executemany(
                    f"INSERT INTO temp.{table} VALUES (?)", batch
                )
                batch.clear()
        if batch:
            connection.executemany(f"INSERT INTO temp.{table} VALUES (?)", batch)

        digest = hashlib.sha256()
        digest.update(
            f"wikilean\0{LOGICAL_JSONL_DOMAIN}\0canonical-artifact-json-v1\0".encode(
                "ascii"
            )
        )
        digest.update(b"[")
        first = True
        for (payload,) in connection.execute(
            f"SELECT payload FROM temp.{table} ORDER BY payload"
        ):
            if not first:
                digest.update(b",")
            digest.update(bytes(payload))
            first = False
        digest.update(b"]")
        return "sha256:" + digest.hexdigest()
    finally:
        with suppress(sqlite3.Error):
            connection.execute(f"DROP TABLE IF EXISTS temp.{table}")


def _sqlite_integrity_check(connection: sqlite3.Connection) -> None:
    """Require a complete SQLite b-tree/index integrity check without buffering errors."""
    try:
        cursor = connection.execute("PRAGMA integrity_check")
        first = cursor.fetchone()
        if first != ("ok",):
            detail = first[0] if first and isinstance(first[0], str) else repr(first)
            _fail("$.artifacts", f"Brain SQLite integrity check failed: {detail}")
        if cursor.fetchone() is not None:
            _fail("$.artifacts", "Brain SQLite integrity check returned unexpected extra rows")
    except VerificationError:
        raise
    except sqlite3.Error as exc:
        raise VerificationError(
            f"$.artifacts: Brain SQLite integrity check failed: {exc}"
        ) from exc


def _verify_sqlite_schema_v2(connection: sqlite3.Connection) -> None:
    """Require the tables, columns, and named query indexes used by schema v2."""
    for table, expected_columns in BRAIN_SQLITE_V2_TABLE_COLUMNS.items():
        actual_columns = tuple(
            row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")')
        )
        if actual_columns != expected_columns:
            _fail(
                "$.artifacts",
                f"Brain SQLite schema v2 table {table!r} has columns "
                f"{actual_columns!r}; expected {expected_columns!r}",
            )

    indexes_by_table: dict[str, dict[str, tuple[Any, ...]]] = {}
    for table in BRAIN_SQLITE_V2_TABLE_COLUMNS:
        indexes_by_table[table] = {
            row[1]: row
            for row in connection.execute(f'PRAGMA index_list("{table}")')
        }
    for index, (table, expected_columns) in BRAIN_SQLITE_V2_INDEXES.items():
        index_row = indexes_by_table[table].get(index)
        if index_row is None:
            _fail(
                "$.artifacts",
                f"Brain SQLite schema v2 is missing required index {index!r}",
            )
        if index_row[2] != 0 or index_row[3] != "c" or index_row[4] != 0:
            _fail(
                "$.artifacts",
                f"Brain SQLite schema v2 index {index!r} must be a non-unique, "
                "non-partial created index",
            )
        actual_key_columns = tuple(
            (row[2], row[3], row[4])
            for row in connection.execute(f'PRAGMA index_xinfo("{index}")')
            if row[5]
        )
        expected_key_columns = tuple(
            (column, 0, "BINARY") for column in expected_columns
        )
        if actual_key_columns != expected_key_columns:
            _fail(
                "$.artifacts",
                f"Brain SQLite schema v2 index {index!r} has key columns "
                f"{actual_key_columns!r}; expected {expected_key_columns!r}",
            )


def _iter_jsonl_objects(handle: BinaryIO, location: str) -> Iterator[dict[str, Any]]:
    """Parse JSONL one row at a time from an already verified artifact handle."""
    handle.seek(0)
    for line_number, raw in enumerate(handle, 1):
        if not raw.strip():
            continue
        yield _expect_object(
            parse_artifact_json_bytes(raw, location=f"{location}:{line_number}"),
            f"{location}:{line_number}",
        )


def _indexed_values(name: str, row: dict[str, Any]) -> tuple[Any, ...]:
    if name == "nodes":
        return row["id"], row["type"], row.get("label")
    if name in {"edges", "edges_links"}:
        return (
            row["src"],
            row["dst"],
            row["kind"],
            row.get("confidence"),
            (row.get("provenance") or {}).get("source"),
        )
    if name == "cells":
        return row["id"], row.get("anchor"), row.get("label")
    if name == "synapses":
        return row["src"], row["dst"], row["weight"]
    raise AssertionError(f"unknown SQLite artifact {name!r}")


def _record_expected_owner(
    connection: sqlite3.Connection,
    organ_id: str,
    owner: str,
    kind: str | None,
    bare: str | None,
    *,
    location: str,
) -> None:
    prior = connection.execute(
        "SELECT owner_id, organ_kind, bare_decl FROM temp.expected_organ_owners "
        "WHERE organ_id = ?",
        (organ_id,),
    ).fetchone()
    if prior is None:
        connection.execute(
            "INSERT INTO temp.expected_organ_owners "
            "(organ_id, owner_id, organ_kind, bare_decl) VALUES (?, ?, ?, ?)",
            (organ_id, owner, kind, bare),
        )
    elif prior[0] != owner:
        _fail(location, f"organ {organ_id!r} has two owners")


def _verify_sqlite_artifact_rows(
    connection: sqlite3.Connection,
    root: Path,
    artifact: dict[str, Any],
    name: str,
    query: str,
    metadata_json: str,
) -> tuple[dict[str, Any], str, int]:
    """Stream one JSONL artifact against its ordinal SQLite index projection."""
    path = artifact["path"]
    location = f"SQLite parity for {path}"
    metadata: dict[str, Any] | None = None
    saw_data = False
    row_count = 0
    row_digest = hashlib.sha256()
    cursor = connection.execute(query)
    with open_verified_file(root, artifact, location) as handle:
        for row in _iter_jsonl_objects(handle, path):
            if set(row) == {"_meta"}:
                if metadata is not None or saw_data:
                    _fail(path, "metadata must be the first non-empty row")
                metadata = _expect_object(row["_meta"], f"{path}._meta")
                continue
            saw_data = True
            row_count += 1
            actual = cursor.fetchone()
            if actual is None or actual[:-1] != _indexed_values(name, row):
                _fail("$.artifacts", f"Brain SQLite indexed columns disagree with {path}")
            payload = actual[-1]
            if not isinstance(payload, str):
                _fail("$.artifacts", f"Brain SQLite payload_json is not text for {path}")
            payload_value = _expect_object(
                parse_artifact_json_bytes(
                    payload.encode("utf-8"), location=f"SQLite payload for {path}"
                ),
                f"SQLite payload for {path}",
            )
            if not _same_logical_json(payload_value, row):
                _fail(
                    "$.artifacts",
                    f"Brain SQLite payload_json is paired with the wrong ordinal/index row for {path}",
                )
            row_digest.update(payload.encode("utf-8"))
            row_digest.update(b"\n")
            if name == "cells":
                for organ in row.get("organs") or []:
                    organ_id = organ.get("id")
                    if not isinstance(organ_id, str):
                        continue
                    kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
                    bare = (
                        organ_id.split(":", 2)[2]
                        if kind == "decl" and organ_id.count(":") >= 2
                        else None
                    )
                    _record_expected_owner(
                        connection,
                        organ_id,
                        row["id"],
                        kind,
                        bare,
                        location=path,
                    )
    if metadata is None:
        _fail(path, "missing metadata row")
    if cursor.fetchone() is not None:
        _fail("$.artifacts", f"Brain SQLite indexed columns disagree with {path}")
    parsed_metadata = parse_json_bytes(
        metadata_json.encode("utf-8"),
        location=f"SQLite artifacts.{name}.metadata_json",
    )
    if not _same_logical_json(parsed_metadata, metadata):
        _fail("$.artifacts", f"Brain SQLite metadata disagrees with {path}")
    logical_digest = hashlib.sha256(
        metadata_json.encode("utf-8")
        + b"\n"
        + row_digest.hexdigest().encode("ascii")
    ).hexdigest()
    return metadata, logical_digest, row_count


def _verify_sqlite_owner_rows(
    connection: sqlite3.Connection, cells_metadata: dict[str, Any]
) -> None:
    # Regular cell ownership has precedence over the fallback ownership map in
    # cells metadata. INSERT OR IGNORE mirrors the builder's setdefault rule.
    for owner, organs in (cells_metadata.get("supercell_organs") or {}).items():
        for organ in organs:
            organ_id = organ.get("id")
            if not isinstance(organ_id, str):
                continue
            kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
            bare = (
                organ_id.split(":", 2)[2]
                if kind == "decl" and organ_id.count(":") >= 2
                else None
            )
            connection.execute(
                "INSERT OR IGNORE INTO temp.expected_organ_owners "
                "(organ_id, owner_id, organ_kind, bare_decl) VALUES (?, ?, ?, ?)",
                (organ_id, owner, kind, bare),
            )

    for actual_table, expected_table in (
        ("organ_owners", "temp.expected_organ_owners"),
        ("temp.expected_organ_owners", "organ_owners"),
    ):
        mismatch = connection.execute(
            f"SELECT organ_id, owner_id, organ_kind, bare_decl FROM {actual_table} "
            "EXCEPT "
            f"SELECT organ_id, owner_id, organ_kind, bare_decl FROM {expected_table} "
            "LIMIT 1"
        ).fetchone()
        if mismatch is not None:
            _fail("$.artifacts", "Brain SQLite organ_owners disagrees with cells.jsonl")


def _verify_sqlite_projection(
    handle: BinaryIO,
    root: Path,
    artifacts: dict[str, dict[str, Any]],
    *,
    verify_static_closure: bool = False,
) -> None:
    # Keep the already hashed descriptor pinned and make SQLite open that exact
    # inode. This avoids both a whole-database bytes object and a temporary copy.
    descriptor_path = Path("/dev/fd") / str(handle.fileno())
    if not descriptor_path.exists():
        _fail("$.artifacts", "this platform cannot safely open a pinned SQLite descriptor")
    uri = descriptor_path.as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA temp_store = FILE")
            _sqlite_integrity_check(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 2:
                _fail(
                    "$.artifacts",
                    f"{RELEASE_PROFILE} requires Brain SQLite schema 2, found {version}",
                )
            if connection.execute("PRAGMA application_id").fetchone()[0] != BRAIN_SQLITE_APPLICATION_ID:
                _fail("$.artifacts", "Brain SQLite application_id is not WLBN")
            _verify_sqlite_schema_v2(connection)
            snapshot = connection.execute(
                "SELECT schema_version, build_state, snapshot_id, "
                "base_snapshot_id, projection_id, metadata_json "
                "FROM snapshot WHERE singleton = 1"
            ).fetchone()
            if snapshot is None:
                _fail("$.artifacts", "Brain SQLite snapshot row is missing")
            (
                snapshot_schema,
                build_state,
                snapshot_id,
                base_snapshot_id,
                projection_id,
                snapshot_metadata_json,
            ) = snapshot
            if snapshot_schema != version:
                _fail("$.artifacts", "Brain SQLite schema versions disagree")
            if build_state != "complete":
                _fail("$.artifacts", "Brain SQLite snapshot is incomplete")
            artifact_records = {
                name: {
                    "digest": digest,
                    "generated_at": generated_at,
                    "row_count": row_count,
                    "source_digest": source_digest,
                    "logical_digest": logical_digest,
                    "raw_digest": raw_digest,
                    "source_present": source_present,
                    "metadata_json": metadata_json,
                }
                for (
                    name,
                    digest,
                    generated_at,
                    row_count,
                    source_digest,
                    logical_digest,
                    raw_digest,
                    source_present,
                    metadata_json,
                ) in connection.execute(
                    "SELECT name, digest, generated_at, row_count, source_digest, logical_digest, "
                    "raw_digest, source_present, metadata_json FROM artifacts"
                )
            }
            projected_roots = {
                "nodes": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM nodes"
                ),
                "edges": _sqlite_payload_root(
                    connection,
                    "SELECT payload_json FROM edges WHERE stream = ?",
                    ("main",),
                ),
                "edges_links": _sqlite_payload_root(
                    connection,
                    "SELECT payload_json FROM edges WHERE stream = ?",
                    ("links",),
                ),
                "cells": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM cells"
                ),
                "synapses": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM synapses"
                ),
            }
            expected_paths = {
                "nodes": "brain/data/nodes.jsonl",
                "edges": "brain/data/edges.jsonl",
                "edges_links": "brain/data/edges_links.jsonl",
                "cells": "brain/data/cells.jsonl",
                "synapses": "brain/data/synapses.jsonl",
            }
            if set(artifact_records) != set(expected_paths):
                _fail(
                    "$.artifacts",
                    "Brain SQLite artifact set does not match the current release profile",
                )
            for name, path in expected_paths.items():
                if artifact_records[name]["source_digest"] != artifacts[path]["sha256"]:
                    _fail("$.artifacts", f"Brain SQLite is stale relative to {path}")
                record = artifact_records[name]
                if record["source_present"] != 1:
                    _fail("$.artifacts", f"Brain SQLite has no raw source for {path}")
                if record["raw_digest"] != artifacts[path]["sha256"]:
                    _fail("$.artifacts", f"Brain SQLite raw digest is stale for {path}")
                if record["source_digest"] != record["raw_digest"]:
                    _fail("$.artifacts", f"Brain SQLite raw digest aliases disagree for {path}")
                if record["digest"] != record["logical_digest"]:
                    _fail(
                        "$.artifacts",
                        f"Brain SQLite logical digest aliases disagree for {path}",
                    )
                if projected_roots[name] != artifacts[path]["logical_root"]:
                    _fail(
                        "$.artifacts",
                        f"Brain SQLite payload rows do not match the logical content of {path}",
                    )

            connection.execute(
                "CREATE TEMP TABLE expected_organ_owners ("
                "organ_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                "organ_kind TEXT, bare_decl TEXT) WITHOUT ROWID"
            )
            queries = {
                "nodes": "SELECT id, type, label, payload_json FROM nodes ORDER BY ordinal",
                "edges": (
                    "SELECT src, dst, kind, confidence, provenance_source, payload_json "
                    "FROM edges WHERE stream = 'main' ORDER BY ordinal"
                ),
                "edges_links": (
                    "SELECT src, dst, kind, confidence, provenance_source, payload_json "
                    "FROM edges WHERE stream = 'links' ORDER BY ordinal"
                ),
                "cells": "SELECT id, anchor, label, payload_json FROM cells ORDER BY ordinal",
                "synapses": "SELECT src, dst, weight, payload_json FROM synapses ORDER BY ordinal",
            }
            cells_metadata: dict[str, Any] | None = None
            logical_digests: dict[str, str] = {}
            for name, path in expected_paths.items():
                parsed_metadata, logical_digest, row_count = _verify_sqlite_artifact_rows(
                    connection,
                    root,
                    artifacts[path],
                    name,
                    queries[name],
                    artifact_records[name]["metadata_json"],
                )
                logical_digests[name] = logical_digest
                if artifact_records[name]["row_count"] != row_count:
                    _fail(
                        "$.artifacts",
                        f"Brain SQLite row_count disagrees with {path}",
                    )
                if artifact_records[name]["generated_at"] != parsed_metadata.get(
                    "generated_at"
                ):
                    _fail(
                        "$.artifacts",
                        f"Brain SQLite generated_at disagrees with {path}",
                    )
                if artifact_records[name]["logical_digest"] != logical_digest:
                    _fail(
                        "$.artifacts",
                        f"Brain SQLite logical digest disagrees with {path}",
                    )
                if name == "cells":
                    cells_metadata = parsed_metadata
            if cells_metadata is None:
                _fail("$.artifacts", "Brain SQLite cells metadata is missing")
            snapshot_metadata = _expect_object(
                parse_json_bytes(
                    snapshot_metadata_json.encode("utf-8"),
                    location="SQLite snapshot.metadata_json",
                ),
                "SQLite snapshot.metadata_json",
            )
            nodes_metadata = parse_json_bytes(
                artifact_records["nodes"]["metadata_json"].encode("utf-8"),
                location="SQLite artifacts.nodes.metadata_json",
            )
            if not _same_logical_json(snapshot_metadata, nodes_metadata):
                _fail(
                    "$.artifacts",
                    "Brain SQLite snapshot metadata disagrees with nodes.jsonl",
                )
            _verify_sqlite_owner_rows(connection, cells_metadata)
            published_base_id = parse_json_bytes(
                artifact_records["nodes"]["metadata_json"].encode("utf-8"),
                location="SQLite artifacts.nodes.metadata_json",
            ).get("snapshot_id")
            if base_snapshot_id != published_base_id:
                _fail(
                    "$.artifacts",
                    "Brain SQLite base_snapshot_id disagrees with nodes.jsonl",
                )
            projection_preimage = {
                "domain": "wikilean-brain-projection-v1",
                "artifacts": [
                    [name, logical_digests[name]]
                    for name in ("nodes", "edges", "edges_links", "cells", "synapses")
                ],
            }
            expected_projection_id = hashlib.sha256(
                canonical_json_bytes(projection_preimage)
            ).hexdigest()
            if snapshot_id != base_snapshot_id:
                _fail(
                    "$.artifacts",
                    "Brain SQLite base snapshot identity aliases disagree",
                )
            if projection_id != expected_projection_id:
                _fail(
                    "$.artifacts",
                    "Brain SQLite projection_id does not bind its complete logical projection",
                )
            if verify_static_closure:
                _verify_current_static_closure(root, artifacts, connection)
        except VerificationError:
            raise
        except sqlite3.Error as exc:
            raise VerificationError(f"$.artifacts: invalid Brain SQLite projection: {exc}") from exc
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise VerificationError(f"$.artifacts: cannot open Brain SQLite projection: {exc}") from exc


def verify_release_files(manifest: dict[str, Any], root: Path) -> dict[str, int]:
    root = root.resolve(strict=True)
    artifact_by_name: dict[str, dict[str, Any]] = {}
    artifacts_by_path = {
        artifact["path"]: artifact for artifact in manifest["artifacts"]
    }
    artifact_paths = set(artifacts_by_path)
    metadata: dict[str, dict[str, Any]] = {}
    sqlite_artifact: dict[str, Any] | None = None
    sqlite_location = "$.artifacts"
    for index, artifact in enumerate(manifest["artifacts"]):
        location = f"$.artifacts[{index}]"
        if artifact["path"] == "brain/data/brain.sqlite3":
            # The SQLite verifier hashes through the pinned descriptor before
            # opening it. Never materialize or copy the database as bytes.
            sqlite_artifact = artifact
            sqlite_location = location
            artifact_by_name[artifact["logical_name"]] = artifact
            continue
        with open_verified_file(root, artifact, location) as handle:
            logical_root = _artifact_logical_root_handle(
                handle, artifact["logical_format"], artifact["path"]
            )
            if artifact["logical_format"] == "json":
                handle.seek(0)
                metadata[artifact["path"]] = _json_meta(
                    handle.read(), artifact["path"]
                )
            elif artifact["logical_format"] == "jsonl-rowset":
                metadata[artifact["path"]] = _jsonl_meta_handle(
                    handle, artifact["path"]
                )
        if logical_root != artifact["logical_root"]:
            _fail(f"{location}.logical_root", f"expected {artifact['logical_root']}, found {logical_root}")
        artifact_by_name[artifact["logical_name"]] = artifact

    if sqlite_artifact is None:
        _fail("$.artifacts", "release does not contain brain/data/brain.sqlite3")
    with open_verified_file(root, sqlite_artifact, sqlite_location) as sqlite_handle:
        _verify_sqlite_projection(
            sqlite_handle,
            root,
            artifacts_by_path,
            verify_static_closure=True,
        )

    allowed_paths = artifact_paths | {ref["path"] for ref in manifest["attestations"]} | {"release.json"}
    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            _fail("$", f"release contains a symlink: {relative}")
        if path.is_file():
            actual_paths.add(relative)
        elif not path.is_dir():
            _fail("$", f"release contains a non-regular entry: {relative}")
    undeclared = sorted(actual_paths - allowed_paths)
    if undeclared:
        _fail("$", f"release contains undeclared files: {', '.join(undeclared)}")

    base_paths = ("brain/data/nodes.jsonl", "brain/data/edges.jsonl", "brain/data/edges_links.jsonl")
    base_generations = {metadata.get(path, {}).get("generated_at") for path in base_paths}
    base_snapshot_ids = {metadata.get(path, {}).get("snapshot_id") for path in base_paths}
    if len(base_generations) != 1 or None in base_generations:
        _fail("$.artifacts", "organ graph JSONL artifacts have mixed or missing generated_at values")
    if len(base_snapshot_ids) != 1 or None in base_snapshot_ids:
        _fail("$.artifacts", "organ graph JSONL artifacts have mixed or missing snapshot_id values")
    cell_paths = (
        "brain/data/cells.jsonl",
        "brain/data/synapses.jsonl",
        "brain/data/frontier.jsonl",
        "brain/data/frontier_graph.json",
        "site/assets/brain/cells/manifest.json",
        "site/assets/brain/cells/aliases.json",
        "site/assets/brain/cells/supercells.json",
        "site/assets/brain/cells/explorer.json",
        "site/assets/brain/cells/frontier_graph.json",
    )
    cell_generations = {metadata.get(path, {}).get("generated_at") for path in cell_paths}
    if len(cell_generations) != 1 or None in cell_generations:
        _fail("$.artifacts", "cell/frontier artifacts have mixed or missing generated_at values")
    base_generation = next(iter(base_generations))
    base_snapshot_id = next(iter(base_snapshot_ids))
    if not isinstance(base_snapshot_id, str) or not DIGEST_RE.fullmatch(base_snapshot_id):
        _fail("$.artifacts", "organ graph snapshot_id must be 64 lowercase SHA-256 hex digits")
    expected_semantic_root = compatibility_semantic_state_root(
        manifest["semantic_epoch"],
        base_snapshot_id,
        {
            path: next(
                artifact["logical_root"]
                for artifact in manifest["artifacts"]
                if artifact["path"] == path
            )
            for path in COMPATIBILITY_SEMANTIC_PATHS
        },
    )
    if manifest["authority"].get("through_changeset") is not None:
        _fail(
            "$.authority.through_changeset",
            "accepted changeset replay verification is not implemented for this release profile",
        )
    if manifest["authority"]["semantic_state_root"] != expected_semantic_root:
        _fail("$.authority.semantic_state_root", f"expected compatibility root {expected_semantic_root}")
    for path in ("brain/data/cells.jsonl", "brain/data/synapses.jsonl"):
        if metadata[path].get("base_generated_at") != base_generation:
            _fail("$.artifacts", f"{path} does not name the organ graph generated_at")
        if metadata[path].get("base_snapshot_id") != base_snapshot_id:
            _fail("$.artifacts", f"{path} does not name the organ graph snapshot_id")

    attestation_count = 0
    for index, ref in enumerate(manifest["attestations"]):
        location = f"$.attestations[{index}]"
        attestation_bytes = verify_file_ref(root, ref, location)
        attestation = parse_json_bytes(attestation_bytes, location=ref["path"])
        if attestation_bytes != canonical_json_bytes(attestation):
            _fail(location, "attestation is not canonical-json-v1 bytes")
        if ref["kind"] == "build":
            if attestation.get("schema") != BUILD_ATTESTATION_SCHEMA_V1:
                _fail(
                    location,
                    f"{RELEASE_PROFILE} requires build-attestation/v1; "
                    "offline replay attestations are not integrated yet",
                )
            validate_build_attestation(attestation)
            by_name = {item["logical_name"]: item for item in attestation["artifacts"]}
            if set(by_name) != set(artifact_by_name):
                _fail(location, "build attestation artifact names do not match the release")
            for name, artifact in artifact_by_name.items():
                attested = by_name[name]
                for field in ("sha256", "bytes", "logical_root"):
                    if attested.get(field) != artifact.get(field):
                        _fail(location, f"build attestation disagrees on {name}.{field}")
            if attestation["input_roots"]["authority"] != manifest["authority"]["semantic_state_root"]:
                _fail(location, "build attestation authority root does not match release")
            if attestation["input_roots"]["source_set"] != manifest["source_set_root"]:
                _fail(location, "build attestation source-set root does not match release")
            if attestation["output_root"] != manifest["authority"]["semantic_state_root"]:
                _fail(location, "build attestation output root does not match release semantic root")
            builder = attestation["builder"]
            reducer = manifest["reducer"]
            for field in ("git_commit", "configuration_sha256", "environment_sha256"):
                if builder[field] != reducer[field]:
                    _fail(location, f"build attestation builder.{field} does not match release reducer.{field}")
        else:
            validate_validation_attestation(attestation)
        if attestation["release_id"] != manifest["release_id"]:
            _fail(location, "attestation release_id does not match release")
        attestation_count += 1

    return {"artifacts": len(manifest["artifacts"]), "attestations": attestation_count}
