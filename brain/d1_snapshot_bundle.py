#!/usr/bin/env python3
"""Independently verify and load one sealed WikiLean D1 snapshot bundle.

This is the single consumer-side verifier shared by annotation mirroring and
community-edge graduation.  It performs no acquisition and accepts only the
closed bundle emitted by :mod:`acquire_d1_snapshot`.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402

BUNDLE_SCHEMA = "wikilean.d1-acquisition-bundle/v1"
CONTROL_SCHEMA = "wikilean.d1-snapshot-control/v1"
NORMALIZATION_SCHEMA = "wikilean.d1-snapshot-normalization/v1"
UPSTREAM_URI = "d1://cloudflare/fc1b0190-77dd-4f41-a5b9-7f30d53df140"
SQL_SHA256 = "ff06ef9e56b56950e76c900cb44324b75f9d93969c3bf3d8f77cd235ae2fd61d"
REQUEST_PARAMETERS_SHA256 = "8c44319321dec3d7b19102548ca14d02ceaa96913fb74a13dcf714be65325cc3"
NORMALIZATION_CONFIGURATION_SHA256 = "369fc2c6b553f4646ad0f46a39e3b9e802c7cfee2b76ff1714907d2263fea5c1"
TOOLCHAIN_SCHEMA = "wikilean.d1-acquisition-toolchain/v1"
CONFIG_SHA256 = "e50553dcf93ed26c59fe703276498e1a111b11073f45bd8ddbcbd1b0083ab317"
D1_BINDING = "SEALED_D1"
WRANGLER_VERSION = "4.120.0"
WRANGLER_INTEGRITY = (
    "sha512-cBmu/MeaB/fPacC0JpATs4duTOCagBxrZo+vBzuTX06tLzwSyAHE1drlHUZ8rP0"
    "VqVz1fy3ReGYTiHdKkoHltg=="
)
WRANGLER_CLI_SHA256 = "9f0469b1e826fd5b76232cd557047fbb30b94e4fd1de65d23e65a3641bd7e7a7"
PACKAGE_LOCK_SHA256 = "533f09a637b9d47ee455da89a1cd14c14cb615fd3fab623a117cb411e874a4b4"
ACQUIRER_WRAPPER_SHA256 = "0fe71d99df5aa47b98e29d9af6be163689ff925b9c8fe7e815e552ba58bd7552"
LOCAL_DEPENDENCY_PINS = (
    {
        "path": "brain/stage_io.py",
        "sha256": "9b659899ce6c62709ac75b8bec2b9d83cd8550281e5d0ca2122ea6a8a805e4cf",
    },
    {
        "path": "brain/tools/authority_contracts.py",
        "sha256": "fb2f105b2cad2a5ceed38925694f8da1766b57774a7e730078f537b68da018c6",
    },
    {
        "path": "brain/tools/execution_environment.py",
        "sha256": "fb447fe288a2948c76037b4b7504eaf73bd04ba6289a2447859a6838d5f81cbd",
    },
)
REQUIRED_PYTHON_STARTUP_FLAGS = {
    "ignore_environment": True,
    "isolated": True,
    "no_site": True,
    "no_user_site": True,
    "safe_path": True,
}
# These values are the reviewed acquisition environment recorded in
# toolchain.json. Update them together with acquire_d1_snapshot.py and its tests.
FORWARDED_ENVIRONMENT = [
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_USER_SERVICE_KEY",
    "CLOUDFLARE_AUTH_USE_KEYRING",
    "CLOUDFLARE_EMAIL",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "USER",
    "WRANGLER_CF_AUTHORIZATION_TOKEN",
    "XDG_CONFIG_HOME",
]
FORCED_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "NO_COLOR": "1",
    "CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV": "false",
    "CLOUDFLARE_TELEMETRY_DISABLED": "true",
    "WRANGLER_CI_DISABLE_CONFIG_WATCHING": "true",
    "WRANGLER_SEND_METRICS": "false",
}
QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")
NODE_VERSION_RE = re.compile(r"^v22\.[0-9]+\.[0-9]+$")
PYTHON_VERSION_RE = re.compile(r"^3\.12\.[0-9]+(?:[+.-][A-Za-z0-9.+-]+)?$")

ARTICLE_FIELDS = (
    "slug", "wikipedia_title", "display_title", "wikidata_qid", "revid",
    "latest_revid", "last_upstream_check", "annotations", "schema_version",
    "version", "n_formalized", "n_partial", "n_not_formalized", "created_at",
    "updated_at",
)
ARTICLE_TABLE_COLUMNS = (
    "slug", "wikipedia_title", "display_title", "wikidata_qid", "revid",
    "annotations", "version", "created_at", "updated_at", "latest_revid",
    "last_upstream_check", "schema_version", "n_formalized", "n_partial",
    "n_not_formalized",
)
EDGE_FIELDS = (
    "id", "src", "dst", "kind", "evidence", "added_by", "actor_type",
    "status", "created_at", "deleted_by", "deleted_at", "version",
)
NODE_FIELDS = (
    "id", "label", "description", "node_type", "added_by", "actor_type",
    "status", "created_at", "deleted_by", "deleted_at", "version",
)
CONTROL_FIELDS = (
    "schema", "articles", "brain_edges", "brain_nodes", "article_columns",
    "brain_edge_columns", "brain_node_columns", "rows_total",
)
RECORD_ORDER = {"article": 1, "brain_edge": 2, "brain_node": 3, "control": 4}

# Must mirror wiki/src/brain-edits.ts COMMUNITY_KINDS + XREF_DBS.
COMMUNITY_KINDS = frozenset(
    {"relates", "xref", "formalizes", "mentions", "matches", "cites"}
)
ACTOR_TYPES = frozenset({"human", "ai"})
ROW_STATUSES = frozenset({"live", "deleted"})
COMMUNITY_NODE_TYPE = "concept"


def _article_filename_key(slug: str, location: str) -> str:
    """Validate a slug can name one unambiguous active mirror sidecar."""
    normalized_filename = unicodedata.normalize("NFD", f"{slug}.json")
    filename_key = normalized_filename.casefold()
    if (
        slug.startswith(".")
        or ".." in slug
        or "/" in slug
        or "\\" in slug
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in slug)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in slug)
        or filename_key == "_meta.json"
        or filename_key.endswith(".agent1.json")
    ):
        raise HarvestError(f"{location}: slug is not mirror-safe")
    if len(normalized_filename.encode("utf-8")) > 255:
        raise HarvestError(f"{location}: mirror filename exceeds 255 UTF-8 bytes")
    return filename_key


NORMALIZATION_SEMANTIC_POLICY = {
    "schema": "wikilean.d1-snapshot-semantic-policy/v1",
    "community": {
        "actor_types": sorted(ACTOR_TYPES),
        "edge_kinds": sorted(COMMUNITY_KINDS),
        "node_id_pattern": QID_RE.pattern,
        "node_type": COMMUNITY_NODE_TYPE,
        "row_statuses": sorted(ROW_STATUSES),
    },
    "article_mirror": {
        "filename_key": "unicode-nfd-then-casefold-slug-dot-json/v1",
        "forbidden_slug_features": [
            "leading-dot",
            "double-dot",
            "forward-slash",
            "backslash",
            "c0-or-del-control",
            "unicode-surrogate",
        ],
        "maximum_nfd_filename_utf8_bytes": 255,
        "minimum_articles": 1,
        "reserved_filename_keys": ["_meta.json"],
        "reserved_filename_suffixes": [".agent1.json"],
    },
}

NORMALIZED_PATHS = {
    "articles": "normalized/articles.jsonl",
    "brain_edges": "normalized/brain_edges.jsonl",
    "brain_nodes": "normalized/brain_nodes.jsonl",
    "control": "normalized/control.json",
}
MEMBER_MEDIA_TYPES = {
    "request.sql": "application/sql",
    "request.json": "application/json",
    "toolchain.json": "application/json",
    "acquired.jsonl": "application/x-ndjson",
    NORMALIZED_PATHS["articles"]: "application/x-ndjson",
    NORMALIZED_PATHS["brain_edges"]: "application/x-ndjson",
    NORMALIZED_PATHS["brain_nodes"]: "application/x-ndjson",
    NORMALIZED_PATHS["control"]: "application/json",
}
MANIFEST_MEMBER_ORDER = tuple(MEMBER_MEDIA_TYPES)
EXPECTED_FILES = frozenset({
    *MEMBER_MEDIA_TYPES,
    "acquisition-receipt.json",
    "normalization-lineage.json",
    "bundle.json",
})
ROOT_ENTRIES = frozenset(
    {path for path in EXPECTED_FILES if "/" not in path} | {"normalized"}
)
NORMALIZED_ENTRIES = frozenset(
    {path.removeprefix("normalized/") for path in EXPECTED_FILES if path.startswith("normalized/")}
)


class SnapshotBundleError(RuntimeError):
    """The sealed D1 bundle is incomplete, altered, or otherwise untrusted."""


# Backward-compatible name while downstream code transitions to the shared API.
HarvestError = SnapshotBundleError


@dataclass(frozen=True)
class SnapshotBundle:
    path: Path
    acquisition_receipt_id: str
    normalization_lineage_id: str
    acquired_at: str
    articles: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    nodes: tuple[dict[str, Any], ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_object(value: Any, fields: Sequence[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarvestError(f"{location}: expected object")
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        raise HarvestError(
            f"{location}: wrong fields "
            f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )
    return value


def _string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise HarvestError(f"{location}: expected {qualifier}string")
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise HarvestError(f"{location}: invalid Unicode string")
    return value


def _nullable_string(value: Any, location: str) -> str | None:
    return None if value is None else _string(value, location)


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= contracts.MAX_SAFE_INTEGER:
        raise HarvestError(f"{location}: expected integer >= {minimum}")
    return value


def _nullable_integer(value: Any, location: str, *, minimum: int = 0) -> int | None:
    return None if value is None else _integer(value, location, minimum=minimum)


def _deleted_state(record: Mapping[str, Any], location: str) -> None:
    status = record["status"]
    if status not in ROW_STATUSES:
        raise HarvestError(f"{location}.status: unknown status {status!r}")
    deleted_by = record["deleted_by"]
    deleted_at = record["deleted_at"]
    if status == "live" and (deleted_by is not None or deleted_at is not None):
        raise HarvestError(f"{location}: live row carries deletion fields")
    if status == "deleted" and (deleted_by is None or deleted_at is None):
        raise HarvestError(f"{location}: deleted row lacks gravestone fields")


def _embedded_json(value: Any, location: str, expected: type) -> Any:
    text = _string(value, location, nonempty=False)
    try:
        parsed = contracts.parse_artifact_json_bytes(text.encode(), location=location)
    except contracts.VerificationError as exc:
        raise HarvestError(str(exc)) from exc
    if not isinstance(parsed, expected):
        raise HarvestError(f"{location}: expected embedded JSON {expected.__name__}")
    return parsed


def _validate_article(value: Any, location: str, *, raw: bool) -> dict[str, Any]:
    row = _exact_object(value, ARTICLE_FIELDS, location)
    for field in ("slug", "wikipedia_title", "display_title"):
        _string(row[field], f"{location}.{field}")
    _article_filename_key(row["slug"], f"{location}.slug")
    _nullable_string(row["wikidata_qid"], f"{location}.wikidata_qid")
    _nullable_integer(row["revid"], f"{location}.revid", minimum=1)
    _nullable_integer(row["latest_revid"], f"{location}.latest_revid", minimum=1)
    _nullable_integer(row["last_upstream_check"], f"{location}.last_upstream_check")
    annotations = _embedded_json(row["annotations"], f"{location}.annotations", list) if raw else row["annotations"]
    if not isinstance(annotations, list):
        raise HarvestError(f"{location}.annotations: expected array")
    _integer(row["schema_version"], f"{location}.schema_version", minimum=1)
    _integer(row["version"], f"{location}.version", minimum=1)
    for field in ("n_formalized", "n_partial", "n_not_formalized"):
        _nullable_integer(row[field], f"{location}.{field}")
    _integer(row["created_at"], f"{location}.created_at")
    _integer(row["updated_at"], f"{location}.updated_at")
    normalized = dict(row)
    normalized["annotations"] = annotations
    return normalized


def _validate_edge_row(value: Any, location: str, *, raw: bool) -> dict[str, Any]:
    row = _exact_object(value, EDGE_FIELDS, location)
    for field in ("id", "src", "dst", "kind", "added_by"):
        _string(row[field], f"{location}.{field}")
    if row["kind"] not in COMMUNITY_KINDS:
        raise HarvestError(f"{location}.kind: unknown community kind {row['kind']!r}")
    if row["actor_type"] not in ACTOR_TYPES:
        raise HarvestError(f"{location}.actor_type: unknown actor {row['actor_type']!r}")
    evidence = _embedded_json(row["evidence"], f"{location}.evidence", dict) if raw else row["evidence"]
    if not isinstance(evidence, dict):
        raise HarvestError(f"{location}.evidence: expected object")
    _string(row["status"], f"{location}.status")
    _integer(row["created_at"], f"{location}.created_at")
    _nullable_string(row["deleted_by"], f"{location}.deleted_by")
    _nullable_integer(row["deleted_at"], f"{location}.deleted_at")
    _integer(row["version"], f"{location}.version", minimum=1)
    _deleted_state(row, location)
    normalized = dict(row)
    normalized["evidence"] = evidence
    return normalized


def _validate_node_row(value: Any, location: str) -> dict[str, Any]:
    row = _exact_object(value, NODE_FIELDS, location)
    for field in ("id", "label", "node_type", "added_by"):
        _string(row[field], f"{location}.{field}")
    if not QID_RE.fullmatch(row["id"]):
        raise HarvestError(f"{location}.id: community node must be a canonical QID")
    if row["node_type"] != COMMUNITY_NODE_TYPE:
        raise HarvestError(
            f"{location}.node_type: expected {COMMUNITY_NODE_TYPE!r}"
        )
    _nullable_string(row["description"], f"{location}.description")
    if row["actor_type"] not in ACTOR_TYPES:
        raise HarvestError(f"{location}.actor_type: unknown actor {row['actor_type']!r}")
    _string(row["status"], f"{location}.status")
    _integer(row["created_at"], f"{location}.created_at")
    _nullable_string(row["deleted_by"], f"{location}.deleted_by")
    _nullable_integer(row["deleted_at"], f"{location}.deleted_at")
    _integer(row["version"], f"{location}.version", minimum=1)
    _deleted_state(row, location)
    return dict(row)


def _validate_control(value: Any, location: str) -> dict[str, Any]:
    row = _exact_object(value, CONTROL_FIELDS, location)
    if row["schema"] != CONTROL_SCHEMA:
        raise HarvestError(f"{location}.schema: unexpected control schema")
    for field in ("articles", "brain_edges", "brain_nodes", "rows_total"):
        _integer(row[field], f"{location}.{field}")
    columns = {
        "article_columns": list(ARTICLE_TABLE_COLUMNS),
        "brain_edge_columns": list(EDGE_FIELDS),
        "brain_node_columns": list(NODE_FIELDS),
    }
    for field, expected in columns.items():
        if row[field] != expected:
            raise HarvestError(f"{location}.{field}: unexpected database columns")
    if row["rows_total"] != row["articles"] + row["brain_edges"] + row["brain_nodes"]:
        raise HarvestError(f"{location}.rows_total: inconsistent total")
    return dict(row)


def _canonical_line(value: Any) -> bytes:
    return contracts.canonical_artifact_json_bytes(value) + b"\n"


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_at(directory_fd: int, name: str, display: str) -> bytes:
    """Read one fixed-name member without resolving its directory by path."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise HarvestError(f"bundle member is not a regular file: {display}")
            if stat.S_IMODE(before.st_mode) != 0o644:
                raise HarvestError(f"bundle member mode is not 0644: {display}")
            chunks: list[bytes] = []
            while block := os.read(descriptor, 1024 * 1024):
                chunks.append(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except HarvestError:
        raise
    except OSError as exc:
        raise HarvestError(f"cannot read bundle member {display}: {exc}") from exc
    if (
        not stat.S_ISREG(after.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or _file_state(before) != _file_state(after)
        or not os.path.samestat(after, linked)
        or after.st_size != sum(map(len, chunks))
    ):
        raise HarvestError(f"bundle member changed while being read: {display}")
    return b"".join(chunks)


def _bundle_bytes(bundle_path: Path) -> dict[str, bytes]:
    """Read the exact bundle tree through held, no-follow directory handles."""
    bundle_path = bundle_path.absolute()
    root_fd = -1
    normalized_fd = -1
    try:
        path_before = bundle_path.lstat()
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_fd = os.open(bundle_path, directory_flags)
        root_meta = os.fstat(root_fd)
        if (
            stat.S_ISLNK(path_before.st_mode)
            or not stat.S_ISDIR(path_before.st_mode)
            or not stat.S_ISDIR(root_meta.st_mode)
            or not os.path.samestat(path_before, root_meta)
        ):
            raise HarvestError(f"snapshot bundle is not a stable real directory: {bundle_path}")
        if stat.S_IMODE(root_meta.st_mode) != 0o700:
            raise HarvestError(f"snapshot bundle directory mode is not 0700: {bundle_path}")
        if frozenset(os.listdir(root_fd)) != ROOT_ENTRIES:
            raise HarvestError("snapshot bundle root member closure mismatch")

        normalized_fd = os.open("normalized", directory_flags, dir_fd=root_fd)
        normalized_meta = os.fstat(normalized_fd)
        normalized_link = os.stat(
            "normalized", dir_fd=root_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(normalized_meta.st_mode)
            or not stat.S_ISDIR(normalized_link.st_mode)
            or not os.path.samestat(normalized_meta, normalized_link)
        ):
            raise HarvestError("bundle normalized member is not a stable real directory")
        if stat.S_IMODE(normalized_meta.st_mode) != 0o700:
            raise HarvestError("bundle normalized directory mode is not 0700")
        if frozenset(os.listdir(normalized_fd)) != NORMALIZED_ENTRIES:
            raise HarvestError("snapshot bundle normalized member closure mismatch")

        files: dict[str, bytes] = {}
        for relative in sorted(EXPECTED_FILES):
            if relative.startswith("normalized/"):
                member_fd = normalized_fd
                name = relative.removeprefix("normalized/")
            else:
                member_fd = root_fd
                name = relative
            files[relative] = _read_regular_at(member_fd, name, relative)

        if frozenset(os.listdir(root_fd)) != ROOT_ENTRIES:
            raise HarvestError("snapshot bundle root closure changed while being read")
        if frozenset(os.listdir(normalized_fd)) != NORMALIZED_ENTRIES:
            raise HarvestError("snapshot bundle normalized closure changed while being read")
        normalized_link = os.stat(
            "normalized", dir_fd=root_fd, follow_symlinks=False
        )
        if not os.path.samestat(normalized_meta, normalized_link):
            raise HarvestError("bundle normalized directory changed while being read")
        path_after = bundle_path.lstat()
        if (
            stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISDIR(path_after.st_mode)
            or not os.path.samestat(root_meta, path_after)
        ):
            raise HarvestError("snapshot bundle directory changed while being read")
        return files
    except OSError as exc:
        raise HarvestError(f"cannot read snapshot bundle {bundle_path}: {exc}") from exc
    finally:
        if normalized_fd >= 0:
            os.close(normalized_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _canonical_json(data: bytes, location: str) -> dict[str, Any]:
    try:
        value = contracts.parse_json_bytes(data, location=location)
    except contracts.VerificationError as exc:
        raise HarvestError(str(exc)) from exc
    if not isinstance(value, dict):
        raise HarvestError(f"{location}: expected object")
    if data != contracts.canonical_json_bytes(value):
        raise HarvestError(f"{location}: expected canonical JSON bytes")
    return value


def _artifact_json(data: bytes, location: str) -> Any:
    try:
        value = contracts.parse_artifact_json_bytes(data, location=location)
    except contracts.VerificationError as exc:
        raise HarvestError(str(exc)) from exc
    if data != contracts.canonical_artifact_json_bytes(value):
        raise HarvestError(f"{location}: expected canonical artifact JSON bytes")
    return value


def _validate_toolchain(value: Any) -> dict[str, Any]:
    toolchain = _exact_object(
        value,
        (
            "schema",
            "invocation",
            "node",
            "python",
            "local_dependencies",
            "wrangler",
            "wrapper",
        ),
        "toolchain.json",
    )
    if toolchain["schema"] != TOOLCHAIN_SCHEMA:
        raise HarvestError("toolchain.json.schema: unexpected schema")
    invocation = _exact_object(
        toolchain["invocation"],
        ("config_sha256", "database_binding", "forwarded_environment", "forced_environment"),
        "toolchain.json.invocation",
    )
    if invocation != {
        "config_sha256": CONFIG_SHA256,
        "database_binding": D1_BINDING,
        "forwarded_environment": FORWARDED_ENVIRONMENT,
        "forced_environment": FORCED_ENVIRONMENT,
    }:
        raise HarvestError("toolchain.json.invocation: unexpected acquisition policy")
    node = _exact_object(toolchain["node"], ("version", "sha256"), "toolchain.json.node")
    if not isinstance(node["version"], str) or not NODE_VERSION_RE.fullmatch(node["version"]):
        raise HarvestError("toolchain.json.node.version: expected recorded Node 22 version")
    if not isinstance(node["sha256"], str) or not contracts.DIGEST_RE.fullmatch(node["sha256"]):
        raise HarvestError("toolchain.json.node.sha256: expected SHA-256 digest")
    python = _exact_object(
        toolchain["python"],
        ("implementation", "version", "sha256", "startup_flags"),
        "toolchain.json.python",
    )
    if python["implementation"] != "CPython":
        raise HarvestError("toolchain.json.python.implementation: expected CPython")
    if (
        not isinstance(python["version"], str)
        or not PYTHON_VERSION_RE.fullmatch(python["version"])
    ):
        raise HarvestError(
            "toolchain.json.python.version: expected recorded Python 3.12 version"
        )
    if (
        not isinstance(python["sha256"], str)
        or not contracts.DIGEST_RE.fullmatch(python["sha256"])
    ):
        raise HarvestError("toolchain.json.python.sha256: expected SHA-256 digest")
    if python["startup_flags"] != REQUIRED_PYTHON_STARTUP_FLAGS:
        raise HarvestError(
            "toolchain.json.python.startup_flags: expected isolated -I -S policy"
        )
    dependencies = toolchain["local_dependencies"]
    if dependencies != list(LOCAL_DEPENDENCY_PINS):
        raise HarvestError(
            "toolchain.json.local_dependencies: does not match reviewed local pins"
        )
    wrangler = _exact_object(
        toolchain["wrangler"],
        ("version", "package_integrity", "cli_sha256", "package_lock_sha256"),
        "toolchain.json.wrangler",
    )
    if wrangler != {
        "version": WRANGLER_VERSION,
        "package_integrity": WRANGLER_INTEGRITY,
        "cli_sha256": WRANGLER_CLI_SHA256,
        "package_lock_sha256": PACKAGE_LOCK_SHA256,
    }:
        raise HarvestError("toolchain.json.wrangler: does not match reviewed pins")
    wrapper = _exact_object(
        toolchain["wrapper"], ("sha256",), "toolchain.json.wrapper"
    )
    if wrapper["sha256"] != ACQUIRER_WRAPPER_SHA256:
        raise HarvestError("toolchain.json.wrapper.sha256: unexpected acquirer wrapper")
    return toolchain


def _raw_normalized_outputs(raw: bytes) -> tuple[dict[str, bytes], dict[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "article": [], "brain_edge": [], "brain_node": [], "control": [],
    }
    seen: set[tuple[str, str]] = set()
    article_filenames: dict[str, str] = {}
    order: list[tuple[int, bytes]] = []
    if raw and not raw.endswith(b"\n"):
        raise HarvestError("acquired.jsonl: missing final newline")
    for index, line in enumerate(raw.splitlines()):
        if not line:
            raise HarvestError(f"acquired.jsonl:{index + 1}: blank line")
        row = _exact_object(
            _artifact_json(line, f"acquired.jsonl:{index + 1}"),
            ("record_type", "record_key", "payload"), f"acquired.jsonl:{index + 1}",
        )
        record_type = _string(row["record_type"], f"acquired.jsonl:{index + 1}.record_type")
        if record_type not in RECORD_ORDER:
            raise HarvestError(f"acquired.jsonl:{index + 1}: unknown record type")
        record_key = _string(row["record_key"], f"acquired.jsonl:{index + 1}.record_key")
        unique = (record_type, record_key)
        if unique in seen:
            raise HarvestError(f"acquired.jsonl:{index + 1}: duplicate record {unique!r}")
        seen.add(unique)
        order.append((RECORD_ORDER[record_type], record_key.encode()))
        payload_text = _string(row["payload"], f"acquired.jsonl:{index + 1}.payload")
        try:
            payload = contracts.parse_artifact_json_bytes(
                payload_text.encode(), location=f"acquired.jsonl:{index + 1}.payload"
            )
        except contracts.VerificationError as exc:
            raise HarvestError(str(exc)) from exc
        location = f"acquired.jsonl:{index + 1}.payload"
        if record_type == "article":
            normalized = _validate_article(payload, location, raw=True)
            if normalized["slug"] != record_key:
                raise HarvestError(f"{location}: slug does not match record key")
            filename_key = _article_filename_key(
                normalized["slug"], f"{location}.slug"
            )
            previous = article_filenames.get(filename_key)
            if previous is not None:
                raise HarvestError(
                    f"{location}: article slugs collide as mirror filenames: "
                    f"{previous!r} and {normalized['slug']!r}"
                )
            article_filenames[filename_key] = normalized["slug"]
        elif record_type == "brain_edge":
            normalized = _validate_edge_row(payload, location, raw=True)
            if normalized["id"] != record_key:
                raise HarvestError(f"{location}: edge id does not match record key")
        elif record_type == "brain_node":
            normalized = _validate_node_row(payload, location)
            if normalized["id"] != record_key:
                raise HarvestError(f"{location}: node id does not match record key")
        else:
            if record_key != "counts":
                raise HarvestError(f"{location}: control key must be 'counts'")
            normalized = _validate_control(payload, location)
        groups[record_type].append(normalized)
    if order != sorted(order):
        raise HarvestError("acquired.jsonl: rows are not in canonical record/key order")
    if len(groups["control"]) != 1:
        raise HarvestError("acquired.jsonl: expected exactly one control row")
    control = groups["control"][0]
    counts = {
        "articles": len(groups["article"]),
        "brain_edges": len(groups["brain_edge"]),
        "brain_nodes": len(groups["brain_node"]),
    }
    for field, count in counts.items():
        if control[field] != count:
            raise HarvestError(f"control {field}={control[field]} but source contains {count}")
    if control["rows_total"] != sum(counts.values()):
        raise HarvestError("control rows_total mismatch")
    if counts["articles"] < 1:
        raise HarvestError("acquired.jsonl: expected at least one article")
    outputs = {
        NORMALIZED_PATHS["articles"]: b"".join(_canonical_line(row) for row in groups["article"]),
        NORMALIZED_PATHS["brain_edges"]: b"".join(_canonical_line(row) for row in groups["brain_edge"]),
        NORMALIZED_PATHS["brain_nodes"]: b"".join(_canonical_line(row) for row in groups["brain_node"]),
        NORMALIZED_PATHS["control"]: contracts.canonical_artifact_json_bytes(control),
    }
    return outputs, groups


def _object_ref(name: str, data: bytes, media_type: str) -> dict[str, Any]:
    return {"object": name, "sha256": _sha256(data), "bytes": len(data), "media_type": media_type}


def verify_snapshot_bundle(bundle_path: Path) -> SnapshotBundle:
    """Load and independently verify one complete D1 acquisition bundle."""
    bundle_path = Path(bundle_path).absolute()
    files = _bundle_bytes(bundle_path)
    manifest = _canonical_json(files["bundle.json"], "bundle.json")
    receipt = _canonical_json(files["acquisition-receipt.json"], "acquisition-receipt.json")
    lineage = _canonical_json(files["normalization-lineage.json"], "normalization-lineage.json")
    toolchain = _canonical_json(files["toolchain.json"], "toolchain.json")
    _validate_toolchain(toolchain)
    try:
        contracts.validate_acquisition_receipt(receipt, location="acquisition-receipt.json")
        contracts.validate_normalization_lineage(lineage, location="normalization-lineage.json")
    except contracts.VerificationError as exc:
        raise HarvestError(str(exc)) from exc

    _exact_object(
        manifest,
        ("schema", "bundle_id", "identity_basis", "request_parameters_sha256",
         "sql_sha256", "toolchain_sha256", "acquisition_receipt_id",
         "normalization_lineage_id", "members", "evidence"),
        "bundle.json",
    )
    if manifest["schema"] != BUNDLE_SCHEMA:
        raise HarvestError("bundle.json.schema: unexpected schema")
    if manifest["identity_basis"] != "normalization_lineage_id":
        raise HarvestError("bundle.json.identity_basis: unexpected identity basis")
    receipt_id = receipt["acquisition_receipt_id"]
    lineage_id = lineage["normalization_lineage_id"]
    if contracts.acquisition_receipt_identity(receipt) != receipt_id:
        raise HarvestError("acquisition receipt identity mismatch")
    if contracts.normalization_lineage_identity(lineage) != lineage_id:
        raise HarvestError("normalization lineage identity mismatch")
    if manifest["bundle_id"] != lineage_id or manifest["normalization_lineage_id"] != lineage_id:
        raise HarvestError("bundle manifest does not bind its lineage identity")
    if manifest["acquisition_receipt_id"] != receipt_id:
        raise HarvestError("bundle manifest does not bind its receipt identity")
    if bundle_path.name != lineage_id.removeprefix("sha256:"):
        raise HarvestError("snapshot bundle directory name is not its lineage identity")
    if manifest["evidence"] != {
        "acquisition_receipt": "acquisition-receipt.json",
        "normalization_lineage": "normalization-lineage.json",
    }:
        raise HarvestError("bundle manifest has unexpected evidence paths")

    members = manifest["members"]
    if not isinstance(members, list) or len(members) != len(MANIFEST_MEMBER_ORDER):
        raise HarvestError("bundle.json.members: unexpected member count")
    for index, (entry, expected_path) in enumerate(zip(members, MANIFEST_MEMBER_ORDER, strict=True)):
        item = _exact_object(entry, ("path", "sha256", "bytes", "media_type"), f"bundle.json.members[{index}]")
        data = files[expected_path]
        expected = {
            "path": expected_path, "sha256": _sha256(data), "bytes": len(data),
            "media_type": MEMBER_MEDIA_TYPES[expected_path],
        }
        if item != expected:
            raise HarvestError(f"bundle.json.members[{index}]: member digest/metadata mismatch")
    if manifest["sql_sha256"] != SQL_SHA256 or _sha256(files["request.sql"]) != SQL_SHA256:
        raise HarvestError("bundle SQL digest mismatch")
    if (
        manifest["request_parameters_sha256"] != REQUEST_PARAMETERS_SHA256
        or _sha256(files["request.json"]) != REQUEST_PARAMETERS_SHA256
    ):
        raise HarvestError("bundle request descriptor digest mismatch")
    if manifest["toolchain_sha256"] != _sha256(files["toolchain.json"]):
        raise HarvestError("bundle toolchain digest mismatch")

    expected_normalized, groups = _raw_normalized_outputs(files["acquired.jsonl"])
    for path, expected in expected_normalized.items():
        if files[path] != expected:
            raise HarvestError(f"{path}: does not exactly normalize acquired.jsonl")

    raw_ref = _object_ref("d1_raw", files["acquired.jsonl"], "application/x-ndjson")
    if receipt["source"] != "wikilean-d1" or receipt["upstream_uri"] != UPSTREAM_URI:
        raise HarvestError("acquisition receipt names an unexpected source")
    if receipt["tool"]["name"] != "wikilean-d1-acquirer" or receipt["tool"]["version"] != "1":
        raise HarvestError("acquisition receipt names an unexpected tool")
    if receipt["outputs"] != [raw_ref]:
        raise HarvestError("acquisition receipt does not bind acquired.jsonl exactly")
    if receipt["pin"] != {"type": "content_sha256", "value": raw_ref["sha256"]}:
        raise HarvestError("acquisition receipt has an unexpected content pin")
    if receipt["tool"]["sha256"] != _sha256(files["toolchain.json"]):
        raise HarvestError("acquisition receipt does not bind toolchain.json")
    if len(receipt["requests"]) != 1 or receipt["requests"][0] != {
        "kind": "database_query", "uri": UPSTREAM_URI,
        "parameters_sha256": manifest["request_parameters_sha256"],
    }:
        raise HarvestError("acquisition receipt does not bind the reviewed D1 request")

    normalized_refs = [
        _object_ref("articles", files[NORMALIZED_PATHS["articles"]], "application/x-ndjson"),
        _object_ref("brain_edges", files[NORMALIZED_PATHS["brain_edges"]], "application/x-ndjson"),
        _object_ref("brain_nodes", files[NORMALIZED_PATHS["brain_nodes"]], "application/x-ndjson"),
        _object_ref("control", files[NORMALIZED_PATHS["control"]], "application/json"),
    ]
    if lineage["source"] != "wikilean-d1" or lineage["mode"] != "transform":
        raise HarvestError("normalization lineage names an unexpected source or mode")
    if lineage["normalization_schema"] != NORMALIZATION_SCHEMA:
        raise HarvestError("normalization lineage names an unexpected schema")
    if lineage["configuration_sha256"] != NORMALIZATION_CONFIGURATION_SHA256:
        raise HarvestError("normalization lineage configuration is not the reviewed transform")
    if (
        lineage["tool"]["name"] != "wikilean-d1-snapshot-normalizer"
        or lineage["tool"]["version"] != "1"
        or lineage["tool"]["sha256"] != ACQUIRER_WRAPPER_SHA256
    ):
        raise HarvestError("normalization lineage names an unexpected tool")
    if lineage["acquisition_receipt_ids"] != [receipt_id] or lineage["parent_source_manifest_ids"] != []:
        raise HarvestError("normalization lineage does not bind exactly one acquisition receipt")
    if lineage["inputs"] != [{**raw_ref, "origin": {"kind": "acquisition_receipt", "id": receipt_id}}]:
        raise HarvestError("normalization lineage does not bind acquired.jsonl exactly")
    if lineage["outputs"] != normalized_refs:
        raise HarvestError("normalization lineage does not bind the exact normalized files")

    return SnapshotBundle(
        path=bundle_path,
        acquisition_receipt_id=receipt_id,
        normalization_lineage_id=lineage_id,
        acquired_at=receipt["audit"]["acquired_at"],
        articles=tuple(groups["article"]),
        edges=tuple(groups["brain_edge"]),
        nodes=tuple(groups["brain_node"]),
    )
