#!/usr/bin/env python3
"""Strict, standard-library verification for WikiLean authority contracts."""
from __future__ import annotations

import copy
import decimal
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

MAX_SAFE_INTEGER = 9_007_199_254_740_991
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MEDIA_TYPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

SOURCE_SCHEMA = "wikilean.source-manifest/v1"
PACK_SCHEMA = "wikilean.offline-pack/v1"
RELEASE_SCHEMA = "wikilean.release/v1"
BUILD_ATTESTATION_SCHEMA = "wikilean.build-attestation/v1"
VALIDATION_ATTESTATION_SCHEMA = "wikilean.validation-attestation/v1"
RELEASE_PROFILE = "brain-current-v1"

SOURCE_DOMAIN = "wikilean.source-manifest.v1"
SOURCE_SET_DOMAIN = "wikilean.source-set.v1"
PACK_DOMAIN = "wikilean.offline-pack.v1"
RELEASE_DOMAIN = "wikilean.release.v1"
BUILD_ATTESTATION_DOMAIN = "wikilean.build-attestation.v1"
VALIDATION_ATTESTATION_DOMAIN = "wikilean.validation-attestation.v1"
LOGICAL_JSON_DOMAIN = "wikilean.logical-json.v1"
LOGICAL_JSONL_DOMAIN = "wikilean.logical-jsonl-rowset.v1"

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


class VerificationError(ValueError):
    """Raised when a contract or referenced local object is invalid."""


def _fail(location: str, message: str) -> None:
    raise VerificationError(f"{location}: {message}")


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


def source_manifest_identity(manifest: dict[str, Any]) -> str:
    value = copy.deepcopy(manifest)
    value.pop("source_manifest_id", None)
    value.pop("audit", None)
    return domain_hash(SOURCE_DOMAIN, value)


def source_set_root(manifest_ids: list[str]) -> str:
    return domain_hash(SOURCE_SET_DOMAIN, sorted(manifest_ids))


def offline_pack_identity(pack: dict[str, Any]) -> str:
    value = copy.deepcopy(pack)
    value.pop("offline_pack_id", None)
    value.pop("audit", None)
    return domain_hash(PACK_DOMAIN, value)


def release_identity(manifest: dict[str, Any]) -> str:
    value = copy.deepcopy(manifest)
    value.pop("release_id", None)
    value.pop("attestations", None)
    value.pop("created_at", None)
    return domain_hash(RELEASE_DOMAIN, value)


def attestation_identity(attestation: dict[str, Any]) -> str:
    schema = attestation.get("schema")
    domains = {
        BUILD_ATTESTATION_SCHEMA: BUILD_ATTESTATION_DOMAIN,
        VALIDATION_ATTESTATION_SCHEMA: VALIDATION_ATTESTATION_DOMAIN,
    }
    if schema not in domains:
        _fail("$.schema", f"unknown attestation schema/version {schema!r}")
    value = copy.deepcopy(attestation)
    value.pop("attestation_id", None)
    value.pop("recorded_at", None)
    return domain_hash(domains[schema], value)


def validate_source_manifest(manifest: Any) -> dict[str, Any]:
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


def validate_offline_pack(pack: Any) -> dict[str, Any]:
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


@contextmanager
def open_verified_file(root: Path, ref: dict[str, Any], location: str) -> Iterator[BinaryIO]:
    """Open a regular file beneath root without following any symlink component."""
    relative = validate_relative_path(ref["path"], f"{location}.path")
    root = root.resolve(strict=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in PurePosixPath(relative).parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        fd = os.open(PurePosixPath(relative).name, file_flags, dir_fd=descriptors[-1])
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(fd)
            _fail(f"{location}.path", f"not a regular file: {relative}")
        handle = os.fdopen(fd, "rb")
        try:
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            if size != ref["bytes"]:
                _fail(f"{location}.bytes", f"expected {ref['bytes']}, found {size} for {relative}")
            if digest.hexdigest() != ref["sha256"]:
                _fail(f"{location}.sha256", f"expected {ref['sha256']}, found {digest.hexdigest()} for {relative}")
            handle.seek(0)
            yield handle
        finally:
            handle.close()
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise VerificationError(f"{location}.path: missing file or directory in {relative}") from exc
    except OSError as exc:
        raise VerificationError(f"{location}.path: cannot safely open {relative}: {exc.strerror or exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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


def verify_source_manifest_files(manifest: dict[str, Any], root: Path) -> int:
    for index, ref in enumerate(manifest["objects"]):
        verify_file_ref(root, ref, f"$.objects[{index}]")
    return len(manifest["objects"])


def verify_offline_pack_files(
    pack: dict[str, Any], root: Path, *, manifest_path: Path | None = None
) -> dict[str, int]:
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
        verify_file_ref(root, ref, location)
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


def logical_jsonl_root(path: Path) -> str:
    return _logical_jsonl_bytes(path.read_bytes(), str(path))


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


def validate_build_attestation(attestation: Any) -> dict[str, Any]:
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


def _routed_shard(value: str, keys: set[str], pad: str) -> str | None:
    matches = [key for key in keys if _normalized_prefix(value, len(key), pad) == key]
    return max(matches, key=len) if matches else None


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
    root: Path, artifact_paths: set[str], artifact_bytes: dict[str, bytes]
) -> None:
    manifest_relative = f"{STATIC_CELLS_PREFIX}manifest.json"
    manifest = parse_artifact_json_bytes(artifact_bytes[manifest_relative], location=manifest_relative)
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

    cell_meta, cell_rows = _jsonl_rows(
        artifact_bytes["brain/data/cells.jsonl"], "brain/data/cells.jsonl"
    )
    _, node_rows = _jsonl_rows(
        artifact_bytes["brain/data/nodes.jsonl"], "brain/data/nodes.jsonl"
    )
    _, edge_rows = _jsonl_rows(
        artifact_bytes["brain/data/edges.jsonl"], "brain/data/edges.jsonl"
    )
    synapse_meta, synapse_rows = _jsonl_rows(
        artifact_bytes["brain/data/synapses.jsonl"], "brain/data/synapses.jsonl"
    )
    frontier_meta, frontier_rows = _jsonl_rows(
        artifact_bytes["brain/data/frontier.jsonl"], "brain/data/frontier.jsonl"
    )
    cells = {
        _expect_string(row.get("id"), "brain/data/cells.jsonl.id"): row
        for row in cell_rows
    }
    if len(cells) != len(cell_rows):
        _fail("brain/data/cells.jsonl", "duplicate cell IDs")
    if manifest_obj.get("prov", []) != cell_meta.get("prov", []):
        _fail(manifest_relative, "prov does not match cells.jsonl metadata")
    expected_trace_prov = synapse_meta.get("prov", [])
    if expected_trace_prov == cell_meta.get("prov", []):
        if "prov" in traces:
            _fail(manifest_relative, "traces.prov must be omitted when provenance tables agree")
    elif traces.get("prov") != expected_trace_prov:
        _fail(manifest_relative, "traces.prov does not match synapses.jsonl metadata")
    cell_prov = cell_meta.get("prov", [])
    if not isinstance(cell_prov, list):
        _fail("brain/data/cells.jsonl._meta.prov", "must be an array")
    trace_prov = expected_trace_prov
    if not isinstance(trace_prov, list):
        _fail("brain/data/synapses.jsonl._meta.prov", "must be an array")
    for index, row in enumerate(cell_rows):
        _validate_provenance_indexes(row, len(cell_prov), f"brain/data/cells.jsonl[{index}]")
    for index, row in enumerate(synapse_rows):
        _validate_provenance_indexes(row, len(trace_prov), f"brain/data/synapses.jsonl[{index}]")

    expected_cell_ids = set(cells)
    nodes = {
        _expect_string(row.get("id"), "brain/data/nodes.jsonl.id"): row
        for row in node_rows
    }
    parent = {
        row["dst"]: row["src"]
        for row in edge_rows
        if row.get("kind") == "contains"
        and isinstance(row.get("dst"), str)
        and row["dst"].startswith("path:")
    }

    def breadcrumb(owner: str | None) -> list[dict[str, str]]:
        chain: list[dict[str, str]] = []
        current = owner
        while current:
            node = nodes.get(current) or {}
            chain.insert(0, {"id": current, "label": node.get("label") or current.split("/")[-1]})
            current = parent.get(current)
        return chain

    by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    expected_trace_values: dict[str, dict[str, Any]] = {}
    for synapse in synapse_rows:
        src = _expect_string(synapse.get("src"), "brain/data/synapses.jsonl.src")
        dst = _expect_string(synapse.get("dst"), "brain/data/synapses.jsonl.dst")
        traces_for_card = [
            _trim_trace(trace)
            for trace in _pick_traces(synapse.get("traces") or [], 6)
        ]
        dropped = len(synapse.get("traces") or []) - len(traces_for_card) + synapse.get("truncated", 0)
        for owner, partner in ((src, dst), (dst, src)):
            entry = {"id": partner, "w": synapse["weight"], "kinds": synapse["kinds"], "traces": traces_for_card}
            if dropped:
                entry["tt"] = len(synapse.get("traces") or []) + synapse.get("truncated", 0)
            by_cell[owner].append(entry)
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

    served_cell_ids: set[str] = set()
    for key, declared_count in shards.items():
        _expect_int(declared_count, f"{manifest_relative}.shards.{key}")
        relative = f"{STATIC_CELLS_PREFIX}{key}.json"
        shard = _expect_object(parse_artifact_json_bytes(artifact_bytes[relative], location=relative), relative)
        if len(shard) != declared_count:
            _fail(relative, f"manifest declares {declared_count} entries, found {len(shard)}")
        for cell_id, entry_value in shard.items():
            routed = _routed_shard(cell_id, set(shards), shard_pad)
            if routed != key:
                _fail(relative, f"cell {cell_id!r} routes to shard {routed!r}, not {key!r}")
            if cell_id in served_cell_ids:
                _fail(relative, f"cell {cell_id!r} appears in more than one static shard")
            served_cell_ids.add(cell_id)
            cell = cells.get(cell_id)
            if cell is None:
                continue
            expected_entry: dict[str, Any] = {
                "cell": {field: value for field, value in cell.items() if field != "organs"},
                "organs": [_organ_payload(organ, nodes) for organ in cell.get("organs") or []],
                "syn": _pick_synapses(by_cell.get(cell_id, []), 200),
                "counts": {
                    "syn": len(by_cell.get(cell_id, [])),
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
            if entry_value != expected_entry:
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
    labels = _expect_array(parse_artifact_json_bytes(artifact_bytes[labels_relative], location=labels_relative), labels_relative)
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
    if labels != expected_labels:
        _fail(labels_relative, "does not match the cells.jsonl search projection")

    aliases_relative = f"{STATIC_CELLS_PREFIX}aliases.json"
    aliases = _expect_object(parse_artifact_json_bytes(artifact_bytes[aliases_relative], location=aliases_relative), aliases_relative)
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

    frontier_source = artifact_bytes["brain/data/frontier_graph.json"]
    frontier_copy = artifact_bytes[f"{STATIC_CELLS_PREFIX}frontier_graph.json"]
    if frontier_copy != frontier_source:
        _fail("$.artifacts", "static frontier_graph.json is not the verbatim Brain data copy")

    explorer_relative = f"{STATIC_CELLS_PREFIX}explorer.json"
    explorer = _expect_object(
        parse_artifact_json_bytes(artifact_bytes[explorer_relative], location=explorer_relative),
        explorer_relative,
    )
    ordered_cells = [cells[cell_id] for cell_id in sorted(cells)]
    cell_index = {cell["id"]: index for index, cell in enumerate(ordered_cells)}
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
    expected_explorer_edges = sorted(
        ([cell_index[row["src"]], cell_index[row["dst"]], row["weight"]]
         for row in synapse_rows
         if row["src"] in cell_index and row["dst"] in cell_index),
        key=lambda value: (-value[2], value[0], value[1]),
    )
    if explorer.get("nodes") != expected_explorer_nodes or explorer.get("edges") != expected_explorer_edges:
        _fail(explorer_relative, "does not match the complete cells/synapses projection")

    supercells_relative = f"{STATIC_CELLS_PREFIX}supercells.json"
    supercells_doc = _expect_object(
        parse_artifact_json_bytes(artifact_bytes[supercells_relative], location=supercells_relative),
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
        if by_cell.get(path_id):
            synapses = by_cell[path_id]
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
    if supercells_doc.get("supercells") != expected_supercells:
        _fail(supercells_relative, "does not match the complete cell/frontier projection")
    expected_roots = sorted(path_id for path_id in expected_supercells if path_id not in parent)
    if supercells_doc.get("roots") != expected_roots:
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
    if manifest_obj.get("roots") != expected_manifest_roots:
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
    if actual_counts != expected_counts:
        _fail(supercells_relative, "metadata counts do not match the complete projection")

    source_registry = _expect_object(
        parse_artifact_json_bytes(
            artifact_bytes["catalog/data/source_registry.json"],
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
        artifact_bytes["site/assets/brain/sources.json"], location="site/assets/brain/sources.json"
    )
    expected_sources_doc = {
        "layers": source_registry["layers"],
        "our_data_license": source_registry["our_data_license"],
        "sources": expected_sources,
    }
    if sources_doc != expected_sources_doc:
        _fail("site/assets/brain/sources.json", "does not match source_registry.json")

    xrefs: dict[str, list[str]] = defaultdict(list)
    for row in edge_rows:
        if row.get("kind") == "xref":
            xrefs[row["dst"]].append(row["src"])
    for line_number, raw in enumerate(artifact_bytes["brain/data/community_edges.jsonl"].splitlines(), 1):
        if not raw.strip():
            continue
        row = _expect_object(
            parse_artifact_json_bytes(raw, location=f"brain/data/community_edges.jsonl:{line_number}"),
            f"brain/data/community_edges.jsonl:{line_number}",
        )
        if row.get("kind") == "xref":
            xrefs[row["dst"]].append(row["src"])
    xref_doc = parse_artifact_json_bytes(
        artifact_bytes["site/assets/brain/xref_index.json"], location="site/assets/brain/xref_index.json"
    )
    if xref_doc != dict(xrefs):
        _fail("site/assets/brain/xref_index.json", "does not match edge inputs")

    served_trace_values: dict[str, dict[str, Any]] = {}
    for key, declared_count in trace_files.items():
        _expect_int(declared_count, f"{manifest_relative}.traces.files.{key}")
        relative = f"{STATIC_CELLS_PREFIX}traces/{key}.json"
        shard = _expect_object(parse_artifact_json_bytes(artifact_bytes[relative], location=relative), relative)
        if len(shard) != declared_count:
            _fail(relative, f"manifest declares {declared_count} entries, found {len(shard)}")
        for pair, trace_value in shard.items():
            routed = _routed_shard(pair, set(trace_files), trace_pad)
            if routed != key:
                _fail(relative, f"trace pair {pair!r} routes to shard {routed!r}, not {key!r}")
            if pair in served_trace_values:
                _fail(relative, f"trace pair {pair!r} appears in more than one shard")
            served_trace_values[pair] = trace_value
    if served_trace_values != expected_trace_values:
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
    digest = hashlib.sha256()
    digest.update(f"wikilean\0{LOGICAL_JSONL_DOMAIN}\0canonical-artifact-json-v1\0".encode("ascii"))
    digest.update(b"[")
    first = True
    for (payload,) in connection.execute(query, parameters):
        if not isinstance(payload, str):
            _fail("$.artifacts", "SQLite payload_json must be text")
        data = payload.encode("utf-8")
        value = parse_artifact_json_bytes(data, location="SQLite payload_json")
        canonical = _decimal_json(value)
        # SQLite stores the builder's compact JSON spelling; exact logical
        # parity is established by the canonical decimal root below.
        if not first:
            digest.update(b",")
        digest.update(canonical)
        first = False
    digest.update(b"]")
    return "sha256:" + digest.hexdigest()


def _verify_sqlite_projection(
    data: bytes,
    artifacts: dict[str, dict[str, Any]],
    artifact_bytes: dict[str, bytes],
) -> None:
    fd, name = tempfile.mkstemp(prefix="wikilean-release-", suffix=".sqlite3")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        uri = Path(name).resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 1:
                _fail("$.artifacts", f"unsupported Brain SQLite schema {version}")
            snapshot = connection.execute(
                "SELECT build_state, snapshot_id FROM snapshot WHERE singleton = 1"
            ).fetchone()
            if snapshot is None or snapshot[0] != "complete":
                _fail("$.artifacts", "Brain SQLite snapshot is incomplete")
            recorded = dict(connection.execute("SELECT name, source_digest FROM artifacts"))
            projected_roots = {
                "nodes": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM nodes ORDER BY payload_json"
                ),
                "edges": _sqlite_payload_root(
                    connection,
                    "SELECT payload_json FROM edges WHERE stream = ? ORDER BY payload_json",
                    ("main",),
                ),
                "edges_links": _sqlite_payload_root(
                    connection,
                    "SELECT payload_json FROM edges WHERE stream = ? ORDER BY payload_json",
                    ("links",),
                ),
                "cells": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM cells ORDER BY payload_json"
                ),
                "synapses": _sqlite_payload_root(
                    connection, "SELECT payload_json FROM synapses ORDER BY payload_json"
                ),
            }
            indexed_rows = {
                "nodes": list(connection.execute(
                    "SELECT id, type, label FROM nodes ORDER BY ordinal"
                )),
                "edges": list(connection.execute(
                    "SELECT src, dst, kind, confidence, provenance_source "
                    "FROM edges WHERE stream = 'main' ORDER BY ordinal"
                )),
                "edges_links": list(connection.execute(
                    "SELECT src, dst, kind, confidence, provenance_source "
                    "FROM edges WHERE stream = 'links' ORDER BY ordinal"
                )),
                "cells": list(connection.execute(
                    "SELECT id, anchor, label FROM cells ORDER BY ordinal"
                )),
                "synapses": list(connection.execute(
                    "SELECT src, dst, weight FROM synapses ORDER BY ordinal"
                )),
            }
            owner_rows = list(connection.execute(
                "SELECT organ_id, owner_id, organ_kind, bare_decl "
                "FROM organ_owners ORDER BY organ_id"
            ))
            artifact_metadata = dict(connection.execute(
                "SELECT name, metadata_json FROM artifacts"
            ))
        except sqlite3.Error as exc:
            raise VerificationError(f"$.artifacts: invalid Brain SQLite projection: {exc}") from exc
        finally:
            connection.close()
    finally:
        Path(name).unlink(missing_ok=True)

    expected_paths = {
        "nodes": "brain/data/nodes.jsonl",
        "edges": "brain/data/edges.jsonl",
        "edges_links": "brain/data/edges_links.jsonl",
        "cells": "brain/data/cells.jsonl",
        "synapses": "brain/data/synapses.jsonl",
    }
    if set(recorded) != set(expected_paths):
        _fail("$.artifacts", "Brain SQLite artifact set does not match the current release profile")
    parsed: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for name, path in expected_paths.items():
        if recorded[name] != artifacts[path]["sha256"]:
            _fail("$.artifacts", f"Brain SQLite is stale relative to {path}")
        if projected_roots[name] != artifacts[path]["logical_root"]:
            _fail(
                "$.artifacts",
                f"Brain SQLite payload rows do not match the logical content of {path}",
            )
        parsed[name] = _jsonl_rows(artifact_bytes[path], path)
        metadata_value = parse_json_bytes(
            artifact_metadata[name].encode("utf-8"),
            location=f"SQLite artifacts.{name}.metadata_json",
        )
        if metadata_value != parsed[name][0]:
            _fail("$.artifacts", f"Brain SQLite metadata disagrees with {path}")

    expected_indexed = {
        "nodes": [
            (row["id"], row["type"], row.get("label"))
            for row in parsed["nodes"][1]
        ],
        "edges": [
            (row["src"], row["dst"], row["kind"], row.get("confidence"),
             (row.get("provenance") or {}).get("source"))
            for row in parsed["edges"][1]
        ],
        "edges_links": [
            (row["src"], row["dst"], row["kind"], row.get("confidence"),
             (row.get("provenance") or {}).get("source"))
            for row in parsed["edges_links"][1]
        ],
        "cells": [
            (row["id"], row.get("anchor"), row.get("label"))
            for row in parsed["cells"][1]
        ],
        "synapses": [
            (row["src"], row["dst"], row["weight"])
            for row in parsed["synapses"][1]
        ],
    }
    for name, expected in expected_indexed.items():
        if indexed_rows[name] != expected:
            _fail("$.artifacts", f"Brain SQLite indexed columns disagree with {expected_paths[name]}")

    expected_owners: dict[str, tuple[str, str | None, str | None]] = {}
    for cell in parsed["cells"][1]:
        for organ in cell.get("organs") or []:
            organ_id = organ.get("id")
            if not isinstance(organ_id, str):
                continue
            kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
            bare = organ_id.split(":", 2)[2] if kind == "decl" and organ_id.count(":") >= 2 else None
            prior = expected_owners.setdefault(organ_id, (cell["id"], kind, bare))
            if prior[0] != cell["id"]:
                _fail("brain/data/cells.jsonl", f"organ {organ_id!r} has two owners")
    for owner, organs in (parsed["cells"][0].get("supercell_organs") or {}).items():
        for organ in organs:
            organ_id = organ.get("id")
            if isinstance(organ_id, str) and organ_id not in expected_owners:
                kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
                bare = organ_id.split(":", 2)[2] if kind == "decl" and organ_id.count(":") >= 2 else None
                expected_owners[organ_id] = (owner, kind, bare)
    expected_owner_rows = [
        (organ_id, owner, kind, bare)
        for organ_id, (owner, kind, bare) in sorted(expected_owners.items())
    ]
    if owner_rows != expected_owner_rows:
        _fail("$.artifacts", "Brain SQLite organ_owners disagrees with cells.jsonl")


def verify_release_files(manifest: dict[str, Any], root: Path) -> dict[str, int]:
    artifact_by_name: dict[str, dict[str, Any]] = {}
    artifact_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    metadata: dict[str, dict[str, Any]] = {}
    artifact_bytes: dict[str, bytes] = {}
    for index, artifact in enumerate(manifest["artifacts"]):
        location = f"$.artifacts[{index}]"
        data = verify_file_ref(root, artifact, location)
        artifact_bytes[artifact["path"]] = data
        logical_root = _artifact_logical_root(data, artifact["logical_format"], artifact["path"])
        if logical_root != artifact["logical_root"]:
            _fail(f"{location}.logical_root", f"expected {artifact['logical_root']}, found {logical_root}")
        artifact_by_name[artifact["logical_name"]] = artifact
        if artifact["logical_format"] == "json":
            metadata[artifact["path"]] = _json_meta(data, artifact["path"])
        elif artifact["logical_format"] == "jsonl-rowset":
            metadata[artifact["path"]] = _jsonl_meta(data, artifact["path"])

    _verify_current_static_closure(root, artifact_paths, artifact_bytes)
    _verify_sqlite_projection(
        artifact_bytes["brain/data/brain.sqlite3"],
        {artifact["path"]: artifact for artifact in manifest["artifacts"]},
        artifact_bytes,
    )

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
            for field in ("version", "git_commit", "configuration_sha256", "environment_sha256"):
                if builder[field] != reducer[field]:
                    _fail(location, f"build attestation builder.{field} does not match release reducer.{field}")
        else:
            validate_validation_attestation(attestation)
        if attestation["release_id"] != manifest["release_id"]:
            _fail(location, "attestation release_id does not match release")
        attestation_count += 1

    return {"artifacts": len(manifest["artifacts"]), "attestations": attestation_count}