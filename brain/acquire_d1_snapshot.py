#!/usr/bin/env python3
"""Acquire one coherent, sealed snapshot of WikiLean's D1 Brain inputs.

The remote boundary is deliberately narrow: one checked-in, read-only SQL
statement is executed by the repository-local, lockfile-pinned Wrangler CLI
through a resolved Node 22 binary and a private, single-database config.
The result contains all article rows, every community edge (including deleted
rows), every community node, and an in-snapshot count control.  Nothing is
published until the complete response has been parsed, type checked, counted,
normalized, and bound to the authority receipt/lineage contracts.

The published directory is named by the clock-free normalization-lineage ID.
Audit timestamps are required by the authority schemas, but are excluded from
their logical identities.  Raw and normalized data never contain wall-clock
values added by this program.  SIGKILL before the single atomic directory
rename can leave a private ``.staging/*.tmp`` orphan, but can never expose a
partial content address.  Such orphans are reported after later CLI runs and
must only be removed when no acquisition process is active.

Remote-command failures publish nothing.  Known Wrangler authentication and
timeout JSON errors are reduced to fixed operator diagnostics; raw stdout and
stderr are never echoed because they may contain query text or credentials.
The supported launcher starts exact CPython 3.12 with ``-I -S``; acquisition
refuses to reach Wrangler when those isolation flags are absent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
TOOLS = ROOT / "brain" / "tools"
if str(BRAIN) not in sys.path:
    sys.path.append(str(BRAIN))
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))

import authority_contracts as contracts  # noqa: E402
import stage_io  # noqa: E402


def _module_origin_mismatch() -> str | None:
    reviewed = (
        ("authority_contracts", contracts, TOOLS / "authority_contracts.py"),
        ("stage_io", stage_io, BRAIN / "stage_io.py"),
        (
            "execution_environment",
            contracts.execution_environment_contract,
            TOOLS / "execution_environment.py",
        ),
    )
    for name, module, expected in reviewed:
        origin = getattr(module, "__file__", None)
        try:
            actual = Path(origin).resolve(strict=True) if origin is not None else None
            reviewed_path = expected.resolve(strict=True)
        except OSError as exc:
            return f"cannot resolve reviewed local module {name}: {exc}"
        if actual != reviewed_path:
            return (
                f"local module {name} loaded from {actual}, expected {reviewed_path}"
            )
    return None


_IMPORT_ORIGIN_MISMATCH = _module_origin_mismatch()
if _IMPORT_ORIGIN_MISMATCH is not None:
    raise ImportError(_IMPORT_ORIGIN_MISMATCH)


REQUEST_PATH = (
    ROOT / "brain" / "authority" / "requests" / "d1-community-snapshot-v1.sql"
)
REQUEST_DESCRIPTOR_PATH = REQUEST_PATH.with_suffix(".json")
SQL_SHA256 = "ff06ef9e56b56950e76c900cb44324b75f9d93969c3bf3d8f77cd235ae2fd61d"
REQUEST_PARAMETERS_SHA256 = (
    "8c44319321dec3d7b19102548ca14d02ceaa96913fb74a13dcf714be65325cc3"
)
D1_ACCOUNT_ID = "02e868c47ebe2b175a0609df94e857e6"
D1_DATABASE_NAME = "wikilean"
D1_DATABASE_ID = "fc1b0190-77dd-4f41-a5b9-7f30d53df140"
D1_BINDING = "SEALED_D1"
UPSTREAM_URI = f"d1://cloudflare/{D1_DATABASE_ID}"
WRANGLER_VERSION = "4.120.0"
WRANGLER_INTEGRITY = (
    "sha512-cBmu/MeaB/fPacC0JpATs4duTOCagBxrZo+vBzuTX06tLzwSyAHE1drlHUZ8rP0"
    "VqVz1fy3ReGYTiHdKkoHltg=="
)
WRANGLER_CLI_SHA256 = (
    "9f0469b1e826fd5b76232cd557047fbb30b94e4fd1de65d23e65a3641bd7e7a7"
)
WRANGLER_CLI = (
    ROOT / "wiki" / "node_modules" / "wrangler" / "wrangler-dist" / "cli.js"
)
PACKAGE_LOCK = ROOT / "wiki" / "package-lock.json"
PACKAGE_LOCK_SHA256 = (
    "533f09a637b9d47ee455da89a1cd14c14cb615fd3fab623a117cb411e874a4b4"
)
DEFAULT_STORE = ROOT / "catalog" / ".cache" / "d1" / "snapshots"
LOCAL_DEPENDENCIES = (
    (
        "brain/stage_io.py",
        BRAIN / "stage_io.py",
        "9b659899ce6c62709ac75b8bec2b9d83cd8550281e5d0ca2122ea6a8a805e4cf",
    ),
    (
        "brain/tools/authority_contracts.py",
        TOOLS / "authority_contracts.py",
        "9cb0d246cce72c173b47bfe9247458ef4a92f1abf3e48a9db7d6484951541d63",
    ),
    (
        "brain/tools/execution_environment.py",
        TOOLS / "execution_environment.py",
        "fb447fe288a2948c76037b4b7504eaf73bd04ba6289a2447859a6838d5f81cbd",
    ),
)
REQUIRED_PYTHON_STARTUP_FLAGS = {
    "ignore_environment": True,
    "isolated": True,
    "no_site": True,
    "no_user_site": True,
    "safe_path": True,
}

BUNDLE_SCHEMA = "wikilean.d1-acquisition-bundle/v1"
CONTROL_SCHEMA = "wikilean.d1-snapshot-control/v1"
NORMALIZATION_SCHEMA = "wikilean.d1-snapshot-normalization/v1"

ARTICLE_FIELDS = (
    "slug",
    "wikipedia_title",
    "display_title",
    "wikidata_qid",
    "revid",
    "latest_revid",
    "last_upstream_check",
    "annotations",
    "schema_version",
    "version",
    "n_formalized",
    "n_partial",
    "n_not_formalized",
    "created_at",
    "updated_at",
)
# SQLite reports table columns in migration order, which deliberately differs
# from the normalized payload order above after migrations 0004 and 0005.
ARTICLE_TABLE_COLUMNS = (
    "slug",
    "wikipedia_title",
    "display_title",
    "wikidata_qid",
    "revid",
    "annotations",
    "version",
    "created_at",
    "updated_at",
    "latest_revid",
    "last_upstream_check",
    "schema_version",
    "n_formalized",
    "n_partial",
    "n_not_formalized",
)
EDGE_FIELDS = (
    "id",
    "src",
    "dst",
    "kind",
    "evidence",
    "added_by",
    "actor_type",
    "status",
    "created_at",
    "deleted_by",
    "deleted_at",
    "version",
)
NODE_FIELDS = (
    "id",
    "label",
    "description",
    "node_type",
    "added_by",
    "actor_type",
    "status",
    "created_at",
    "deleted_by",
    "deleted_at",
    "version",
)
CONTROL_FIELDS = (
    "schema",
    "articles",
    "brain_edges",
    "brain_nodes",
    "article_columns",
    "brain_edge_columns",
    "brain_node_columns",
    "rows_total",
)
RECORD_ORDER = {"article": 1, "brain_edge": 2, "brain_node": 3, "control": 4}
COMMUNITY_KINDS = frozenset(
    {"relates", "xref", "formalizes", "mentions", "matches", "cites"}
)
ACTOR_TYPES = frozenset({"human", "ai"})
ROW_STATUSES = frozenset({"live", "deleted"})
COMMUNITY_NODE_TYPE = "concept"
QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")


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
        raise D1SnapshotError(f"{location}: slug is not mirror-safe")
    if len(normalized_filename.encode("utf-8")) > 255:
        raise D1SnapshotError(f"{location}: mirror filename exceeds 255 UTF-8 bytes")
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

NORMALIZATION_CONFIGURATION = {
    "schema": NORMALIZATION_SCHEMA,
    "request_parameters_sha256": REQUEST_PARAMETERS_SHA256,
    "sql_sha256": SQL_SHA256,
    "raw_record_fields": ["record_type", "record_key", "payload"],
    "record_order": ["article", "brain_edge", "brain_node", "control"],
    "article_fields": list(ARTICLE_FIELDS),
    "article_table_columns": list(ARTICLE_TABLE_COLUMNS),
    "brain_edge_fields": list(EDGE_FIELDS),
    "brain_node_fields": list(NODE_FIELDS),
    "control_fields": list(CONTROL_FIELDS),
    "embedded_json_fields": {
        "article": ["annotations"],
        "brain_edge": ["evidence"],
    },
    "semantic_policy": NORMALIZATION_SEMANTIC_POLICY,
}
NORMALIZATION_CONFIGURATION_SHA256 = hashlib.sha256(
    contracts.canonical_json_bytes(NORMALIZATION_CONFIGURATION)
).hexdigest()


class D1SnapshotError(RuntimeError):
    """The D1 response or local acquisition environment is not trustworthy."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


LOADED_SCRIPT_SHA256 = _file_sha256(Path(__file__))


def _python_startup_flags() -> dict[str, bool]:
    return {
        name: bool(getattr(sys.flags, name, False))
        for name in REQUIRED_PYTHON_STARTUP_FLAGS
    }


def _verify_isolated_startup() -> dict[str, bool]:
    flags = _python_startup_flags()
    if flags != REQUIRED_PYTHON_STARTUP_FLAGS:
        raise D1SnapshotError(
            "D1 acquisition requires the supported isolated CPython 3.12 "
            "launcher (-I -S)"
        )
    return flags


def _verify_local_module_origins() -> None:
    mismatch = _module_origin_mismatch()
    if mismatch is not None:
        raise D1SnapshotError(mismatch)


def _verify_loaded_program() -> None:
    if _file_sha256(Path(__file__)) != LOADED_SCRIPT_SHA256:
        raise D1SnapshotError("D1 snapshot program changed during acquisition")


def _canonical_line(value: Any) -> bytes:
    return contracts.canonical_artifact_json_bytes(value) + b"\n"


def _request_bytes() -> bytes:
    data = REQUEST_PATH.read_bytes()
    actual = _sha256(data)
    if actual != SQL_SHA256:
        raise D1SnapshotError(
            f"D1 SQL preimage changed: expected {SQL_SHA256}, got {actual}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise D1SnapshotError("D1 request preimage is not UTF-8") from exc
    # The reviewed preimage intentionally contains no statement separator.  This
    # keeps Wrangler's invocation to exactly one SQLite read transaction.
    if ";" in text:
        raise D1SnapshotError("D1 request preimage must contain exactly one statement")
    if not text.lstrip().upper().startswith("WITH "):
        raise D1SnapshotError("D1 request preimage must be a CTE query")
    return data


def _request_descriptor_bytes() -> bytes:
    data = REQUEST_DESCRIPTOR_PATH.read_bytes()
    actual = _sha256(data)
    if actual != REQUEST_PARAMETERS_SHA256:
        raise D1SnapshotError(
            "D1 request descriptor changed: "
            f"expected {REQUEST_PARAMETERS_SHA256}, got {actual}"
        )
    try:
        descriptor = contracts.parse_json_bytes(
            data, location=str(REQUEST_DESCRIPTOR_PATH)
        )
    except contracts.VerificationError as exc:
        raise D1SnapshotError(str(exc)) from exc
    expected = {
        "account_id": D1_ACCOUNT_ID,
        "database_id": D1_DATABASE_ID,
        "database_name": D1_DATABASE_NAME,
        "schema": "wikilean.d1-query-request/v1",
        "sql_path": REQUEST_PATH.relative_to(ROOT).as_posix(),
        "sql_sha256": SQL_SHA256,
    }
    if descriptor != expected:
        raise D1SnapshotError("D1 request descriptor does not match reviewed constants")
    return data


def _strict_json(data: str, *, location: str) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise D1SnapshotError(f"{location}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise D1SnapshotError(f"{location}: non-finite JSON number {value!r}")

    try:
        return json.loads(
            data,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except D1SnapshotError:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        raise D1SnapshotError(f"{location}: invalid JSON: {exc}") from exc


def _exact_object(value: Any, fields: Sequence[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise D1SnapshotError(f"{location}: expected object")
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise D1SnapshotError(
            f"{location}: wrong fields (missing={missing}, extra={extra})"
        )
    return value


def _string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        suffix = " non-empty" if nonempty else ""
        raise D1SnapshotError(f"{location}: expected{suffix} string")
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise D1SnapshotError(f"{location}: contains invalid Unicode")
    return value


def _nullable_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _string(value, location)


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > contracts.MAX_SAFE_INTEGER:
        raise D1SnapshotError(
            f"{location}: expected integer in [{minimum}, {contracts.MAX_SAFE_INTEGER}]"
        )
    return value


def _nullable_integer(value: Any, location: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _integer(value, location, minimum=minimum)


def _embedded_json(value: Any, location: str, *, expected: type) -> Any:
    text = _string(value, location, nonempty=False)
    try:
        parsed = contracts.parse_artifact_json_bytes(
            text.encode("utf-8"), location=location
        )
    except contracts.VerificationError as exc:
        raise D1SnapshotError(str(exc)) from exc
    if not isinstance(parsed, expected):
        raise D1SnapshotError(f"{location}: expected embedded JSON {expected.__name__}")
    return parsed


def _validate_deleted_state(record: Mapping[str, Any], location: str) -> None:
    status = record["status"]
    if status not in ROW_STATUSES:
        raise D1SnapshotError(f"{location}.status: expected live or deleted")
    deleted_by = record["deleted_by"]
    deleted_at = record["deleted_at"]
    if status == "live" and (deleted_by is not None or deleted_at is not None):
        raise D1SnapshotError(f"{location}: live row carries deletion fields")
    if status == "deleted" and (deleted_by is None or deleted_at is None):
        raise D1SnapshotError(f"{location}: deleted row lacks its gravestone fields")


def _validate_article(record: Any, location: str) -> dict[str, Any]:
    obj = _exact_object(record, ARTICLE_FIELDS, location)
    _string(obj["slug"], f"{location}.slug")
    _article_filename_key(obj["slug"], f"{location}.slug")
    _string(obj["wikipedia_title"], f"{location}.wikipedia_title")
    _string(obj["display_title"], f"{location}.display_title")
    _nullable_string(obj["wikidata_qid"], f"{location}.wikidata_qid")
    _nullable_integer(obj["revid"], f"{location}.revid", minimum=1)
    _nullable_integer(obj["latest_revid"], f"{location}.latest_revid", minimum=1)
    _nullable_integer(
        obj["last_upstream_check"], f"{location}.last_upstream_check"
    )
    annotations = _embedded_json(
        obj["annotations"], f"{location}.annotations", expected=list
    )
    _integer(obj["schema_version"], f"{location}.schema_version", minimum=1)
    _integer(obj["version"], f"{location}.version", minimum=1)
    for field in ("n_formalized", "n_partial", "n_not_formalized"):
        _nullable_integer(obj[field], f"{location}.{field}")
    _integer(obj["created_at"], f"{location}.created_at")
    _integer(obj["updated_at"], f"{location}.updated_at")
    normalized = dict(obj)
    normalized["annotations"] = annotations
    return normalized


def _validate_edge(record: Any, location: str) -> dict[str, Any]:
    obj = _exact_object(record, EDGE_FIELDS, location)
    for field in ("id", "src", "dst", "kind", "added_by"):
        _string(obj[field], f"{location}.{field}")
    if obj["kind"] not in COMMUNITY_KINDS:
        raise D1SnapshotError(f"{location}.kind: unexpected community kind")
    if obj["actor_type"] not in ACTOR_TYPES:
        raise D1SnapshotError(f"{location}.actor_type: expected human or ai")
    evidence = _embedded_json(
        obj["evidence"], f"{location}.evidence", expected=dict
    )
    _string(obj["status"], f"{location}.status")
    _integer(obj["created_at"], f"{location}.created_at")
    _nullable_string(obj["deleted_by"], f"{location}.deleted_by")
    _nullable_integer(obj["deleted_at"], f"{location}.deleted_at")
    _integer(obj["version"], f"{location}.version", minimum=1)
    _validate_deleted_state(obj, location)
    normalized = dict(obj)
    normalized["evidence"] = evidence
    return normalized


def _validate_node(record: Any, location: str) -> dict[str, Any]:
    obj = _exact_object(record, NODE_FIELDS, location)
    for field in ("id", "label", "node_type", "added_by"):
        _string(obj[field], f"{location}.{field}")
    if not QID_RE.fullmatch(obj["id"]):
        raise D1SnapshotError(f"{location}.id: expected canonical QID")
    if obj["node_type"] != COMMUNITY_NODE_TYPE:
        raise D1SnapshotError(
            f"{location}.node_type: expected {COMMUNITY_NODE_TYPE!r}"
        )
    _nullable_string(obj["description"], f"{location}.description")
    if obj["actor_type"] not in ACTOR_TYPES:
        raise D1SnapshotError(f"{location}.actor_type: expected human or ai")
    _string(obj["status"], f"{location}.status")
    _integer(obj["created_at"], f"{location}.created_at")
    _nullable_string(obj["deleted_by"], f"{location}.deleted_by")
    _nullable_integer(obj["deleted_at"], f"{location}.deleted_at")
    _integer(obj["version"], f"{location}.version", minimum=1)
    _validate_deleted_state(obj, location)
    return dict(obj)


def _validate_control(record: Any, location: str) -> dict[str, Any]:
    obj = _exact_object(record, CONTROL_FIELDS, location)
    if obj["schema"] != CONTROL_SCHEMA:
        raise D1SnapshotError(f"{location}.schema: unexpected control schema")
    for field in ("articles", "brain_edges", "brain_nodes", "rows_total"):
        _integer(obj[field], f"{location}.{field}")
    expected_columns = {
        "article_columns": list(ARTICLE_TABLE_COLUMNS),
        "brain_edge_columns": list(EDGE_FIELDS),
        "brain_node_columns": list(NODE_FIELDS),
    }
    for field, expected in expected_columns.items():
        if obj[field] != expected:
            raise D1SnapshotError(
                f"{location}.{field}: database schema does not match reviewed columns"
            )
    expected_total = obj["articles"] + obj["brain_edges"] + obj["brain_nodes"]
    if obj["rows_total"] != expected_total:
        raise D1SnapshotError(f"{location}.rows_total: inconsistent control total")
    return dict(obj)


def parse_wrangler_output(stdout: str) -> list[dict[str, Any]]:
    """Validate one Wrangler statement result and return its canonical rows."""
    parsed = _strict_json(stdout, location="wrangler stdout")
    if not isinstance(parsed, list) or len(parsed) != 1:
        raise D1SnapshotError("wrangler stdout: expected exactly one statement result")
    statement = parsed[0]
    if not isinstance(statement, dict):
        raise D1SnapshotError("wrangler stdout[0]: expected object")
    if statement.get("success") is not True:
        raise D1SnapshotError("wrangler stdout[0].success: expected true")
    rows = statement.get("results")
    if not isinstance(rows, list):
        raise D1SnapshotError("wrangler stdout[0].results: expected array")

    canonical_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    normalized: dict[str, list[dict[str, Any]]] = {
        "article": [],
        "brain_edge": [],
        "brain_node": [],
        "control": [],
    }
    article_filenames: dict[str, str] = {}
    for index, row in enumerate(rows):
        location = f"wrangler stdout[0].results[{index}]"
        obj = _exact_object(row, ("record_type", "record_key", "payload"), location)
        record_type = _string(obj["record_type"], f"{location}.record_type")
        if record_type not in RECORD_ORDER:
            raise D1SnapshotError(f"{location}.record_type: unknown record type")
        record_key = _string(obj["record_key"], f"{location}.record_key")
        key = (record_type, record_key)
        if key in seen:
            raise D1SnapshotError(f"{location}: duplicate record {key!r}")
        seen.add(key)
        payload_text = _string(obj["payload"], f"{location}.payload")
        try:
            payload = contracts.parse_artifact_json_bytes(
                payload_text.encode("utf-8"), location=f"{location}.payload"
            )
        except contracts.VerificationError as exc:
            raise D1SnapshotError(str(exc)) from exc

        if record_type == "article":
            value = _validate_article(payload, f"{location}.payload")
            if value["slug"] != record_key:
                raise D1SnapshotError(f"{location}: article key does not match slug")
            filename_key = _article_filename_key(
                value["slug"], f"{location}.payload.slug"
            )
            previous = article_filenames.get(filename_key)
            if previous is not None:
                raise D1SnapshotError(
                    f"{location}: article slugs collide as mirror filenames: "
                    f"{previous!r} and {value['slug']!r}"
                )
            article_filenames[filename_key] = value["slug"]
        elif record_type == "brain_edge":
            value = _validate_edge(payload, f"{location}.payload")
            if value["id"] != record_key:
                raise D1SnapshotError(f"{location}: edge key does not match id")
        elif record_type == "brain_node":
            value = _validate_node(payload, f"{location}.payload")
            if value["id"] != record_key:
                raise D1SnapshotError(f"{location}: node key does not match id")
        else:
            if record_key != "counts":
                raise D1SnapshotError(f"{location}: unknown control record")
            value = _validate_control(payload, f"{location}.payload")

        normalized[record_type].append(value)
        canonical_rows.append(
            {
                "record_type": record_type,
                "record_key": record_key,
                "payload": payload_text,
            }
        )

    if len(normalized["control"]) != 1:
        raise D1SnapshotError("wrangler result requires exactly one count control")
    control = normalized["control"][0]
    actual = {
        "articles": len(normalized["article"]),
        "brain_edges": len(normalized["brain_edge"]),
        "brain_nodes": len(normalized["brain_node"]),
    }
    for field, count in actual.items():
        if control[field] != count:
            raise D1SnapshotError(
                f"count control {field}={control[field]} but received {count} rows"
            )
    if control["rows_total"] != sum(actual.values()):
        raise D1SnapshotError("count control rows_total does not match received rows")
    if actual["articles"] < 1:
        raise D1SnapshotError("wrangler result requires at least one article")

    canonical_rows.sort(
        key=lambda item: (
            RECORD_ORDER[item["record_type"]],
            item["record_key"].encode("utf-8"),
        )
    )
    return canonical_rows


def _normalization_outputs(
    canonical_rows: Sequence[dict[str, Any]],
) -> dict[str, bytes]:
    groups: dict[str, list[dict[str, Any]]] = {
        "article": [],
        "brain_edge": [],
        "brain_node": [],
        "control": [],
    }
    raw = bytearray()
    for index, row in enumerate(canonical_rows):
        raw.extend(_canonical_line(row))
        payload = contracts.parse_artifact_json_bytes(
            row["payload"].encode("utf-8"), location=f"canonical row {index}"
        )
        record_type = row["record_type"]
        if record_type == "article":
            payload = _validate_article(payload, f"canonical row {index}")
        elif record_type == "brain_edge":
            payload = _validate_edge(payload, f"canonical row {index}")
        elif record_type == "brain_node":
            payload = _validate_node(payload, f"canonical row {index}")
        else:
            payload = _validate_control(payload, f"canonical row {index}")
        groups[record_type].append(payload)

    return {
        "acquired.jsonl": bytes(raw),
        "normalized/articles.jsonl": b"".join(
            _canonical_line(value) for value in groups["article"]
        ),
        "normalized/brain_edges.jsonl": b"".join(
            _canonical_line(value) for value in groups["brain_edge"]
        ),
        "normalized/brain_nodes.jsonl": b"".join(
            _canonical_line(value) for value in groups["brain_node"]
        ),
        "normalized/control.json": contracts.canonical_artifact_json_bytes(
            groups["control"][0]
        ),
    }


def _object_ref(name: str, data: bytes, media_type: str) -> dict[str, Any]:
    return {
        "object": name,
        "sha256": _sha256(data),
        "bytes": len(data),
        "media_type": media_type,
    }


_AUTH_ENVIRONMENT = (
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_EMAIL",
    "CLOUDFLARE_API_USER_SERVICE_KEY",
    "WRANGLER_CF_AUTHORIZATION_TOKEN",
    "CLOUDFLARE_AUTH_USE_KEYRING",
)
_BASIC_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "XDG_CONFIG_HOME", "LANG", "LC_ALL")
_FORCED_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "NO_COLOR": "1",
    "CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV": "false",
    "CLOUDFLARE_TELEMETRY_DISABLED": "true",
    "WRANGLER_CI_DISABLE_CONFIG_WATCHING": "true",
    "WRANGLER_SEND_METRICS": "false",
}


def _sanitized_subprocess_environment() -> dict[str, str]:
    """Build a small environment with auth, but no code/endpoint injection."""
    environment = {
        name: os.environ[name]
        for name in (*_BASIC_ENVIRONMENT, *_AUTH_ENVIRONMENT)
        if name in os.environ
    }
    environment.update(_FORCED_ENVIRONMENT)
    return environment


def _resolved_node() -> Path:
    candidate = shutil.which("node")
    if candidate is None:
        raise D1SnapshotError("Node 22 is absent from PATH")
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise D1SnapshotError("cannot resolve the Node executable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise D1SnapshotError(f"Node executable is not a regular executable: {resolved}")
    return resolved


def _resolved_python() -> Path:
    if platform.python_implementation() != "CPython":
        raise D1SnapshotError("D1 acquisition requires CPython 3.12")
    if sys.version_info[:2] != (3, 12):
        raise D1SnapshotError(
            f"D1 acquisition requires Python 3.12, found {platform.python_version()}"
        )
    try:
        resolved = Path(sys.executable).resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise D1SnapshotError("cannot resolve the Python executable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise D1SnapshotError(
            f"Python executable is not a regular executable: {resolved}"
        )
    return resolved


def _local_dependency_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for relative, path, expected in LOCAL_DEPENDENCIES:
        actual = _file_sha256(path)
        if actual != expected:
            raise D1SnapshotError(
                f"D1 acquisition dependency changed: {relative}; "
                f"expected {expected}, got {actual}"
            )
        records.append({"path": relative, "sha256": actual})
    return records


def _verify_runtime_closure(toolchain: Mapping[str, Any]) -> None:
    """Recheck every Python-side executable/code byte bound by toolchain.json."""
    _verify_loaded_program()
    _verify_local_module_origins()
    python = _resolved_python()
    expected_python = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "sha256": _file_sha256(python),
    }
    recorded_python = toolchain.get("python")
    if not isinstance(recorded_python, Mapping) or {
        key: recorded_python.get(key) for key in expected_python
    } != expected_python:
        raise D1SnapshotError("Python executable changed during D1 acquisition")
    if recorded_python.get("startup_flags") != REQUIRED_PYTHON_STARTUP_FLAGS:
        raise D1SnapshotError("D1 acquisition toolchain lacks isolated startup flags")
    if toolchain.get("local_dependencies") != _local_dependency_records():
        raise D1SnapshotError("local dependency closure changed during D1 acquisition")
    if toolchain.get("wrapper") != {"sha256": LOADED_SCRIPT_SHA256}:
        raise D1SnapshotError("D1 snapshot wrapper changed during acquisition")


def _pinned_toolchain(
    *,
    probe_runner: Runner = subprocess.run,
    environment: Mapping[str, str],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    startup_flags = _verify_isolated_startup()
    _verify_local_module_origins()
    if not PACKAGE_LOCK.is_file():
        raise D1SnapshotError(f"missing package lock: {PACKAGE_LOCK}")
    lock_bytes = PACKAGE_LOCK.read_bytes()
    lock_sha256 = _sha256(lock_bytes)
    if lock_sha256 != PACKAGE_LOCK_SHA256:
        raise D1SnapshotError(
            f"package lock changed: expected {PACKAGE_LOCK_SHA256}, got {lock_sha256}"
        )
    try:
        lock = contracts.parse_json_bytes(
            lock_bytes, location=str(PACKAGE_LOCK)
        )
    except contracts.VerificationError as exc:
        raise D1SnapshotError(str(exc)) from exc
    try:
        root_version = lock["packages"][""]["devDependencies"]["wrangler"]
        package = lock["packages"]["node_modules/wrangler"]
        package_version = package["version"]
        package_integrity = package["integrity"]
    except (KeyError, TypeError) as exc:
        raise D1SnapshotError("package lock lacks the pinned Wrangler package") from exc
    if root_version != WRANGLER_VERSION or package_version != WRANGLER_VERSION:
        raise D1SnapshotError(
            f"Wrangler lock mismatch: expected exact {WRANGLER_VERSION}, "
            f"got root={root_version!r}, package={package_version!r}"
        )
    if package_integrity != WRANGLER_INTEGRITY:
        raise D1SnapshotError("Wrangler lockfile integrity does not match reviewed pin")
    if not WRANGLER_CLI.is_file():
        raise D1SnapshotError("pinned local Wrangler is absent; run `cd wiki && npm ci`")
    cli_metadata = WRANGLER_CLI.lstat()
    if stat.S_ISLNK(cli_metadata.st_mode) or not stat.S_ISREG(cli_metadata.st_mode):
        raise D1SnapshotError("local Wrangler CLI is not a regular file")
    cli_sha256 = _file_sha256(WRANGLER_CLI)
    if cli_sha256 != WRANGLER_CLI_SHA256:
        raise D1SnapshotError("local Wrangler CLI bytes do not match reviewed pin")

    node = _resolved_node()
    node_sha256 = _file_sha256(node)
    probe = probe_runner(
        [str(node), "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=dict(environment),
    )
    if probe.returncode != 0 or not isinstance(probe.stdout, str):
        raise D1SnapshotError("resolved Node executable failed its version probe")
    node_version = probe.stdout.strip()
    if not re.fullmatch(r"v22\.[0-9]+\.[0-9]+", node_version):
        raise D1SnapshotError(f"Node 22 required, found {node_version!r}")
    python = _resolved_python()
    toolchain = {
        "schema": "wikilean.d1-acquisition-toolchain/v1",
        "invocation": {
            "config_sha256": _sha256(_minimal_wrangler_config()),
            "database_binding": D1_BINDING,
            "forwarded_environment": sorted(
                {*_BASIC_ENVIRONMENT, *_AUTH_ENVIRONMENT}
            ),
            "forced_environment": dict(_FORCED_ENVIRONMENT),
        },
        "node": {"version": node_version, "sha256": node_sha256},
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "sha256": _file_sha256(python),
            "startup_flags": startup_flags,
        },
        "local_dependencies": _local_dependency_records(),
        "wrangler": {
            "version": WRANGLER_VERSION,
            "package_integrity": WRANGLER_INTEGRITY,
            "cli_sha256": cli_sha256,
            "package_lock_sha256": lock_sha256,
        },
        "wrapper": {"sha256": LOADED_SCRIPT_SHA256},
    }
    toolchain_bytes = contracts.canonical_json_bytes(toolchain)
    tool = {
        "name": "wikilean-d1-acquirer",
        "version": "1",
        "sha256": _sha256(toolchain_bytes),
    }
    _verify_runtime_closure(toolchain)
    return node, tool, toolchain


def _minimal_wrangler_config() -> bytes:
    return contracts.canonical_json_bytes(
        {
            "account_id": D1_ACCOUNT_ID,
            "d1_databases": [
                {
                    "binding": D1_BINDING,
                    "database_name": D1_DATABASE_NAME,
                    "database_id": D1_DATABASE_ID,
                }
            ],
            "name": "wikilean-d1-acquisition",
        }
    )


def _same_regular_file(path: Path, expected: os.stat_result, expected_bytes: bytes) -> bool:
    try:
        before = path.lstat()
        data = path.read_bytes()
        after = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(before.st_mode)
        and not stat.S_ISLNK(before.st_mode)
        and stat.S_IMODE(before.st_mode) == 0o600
        and os.path.samestat(expected, before)
        and os.path.samestat(before, after)
        and data == expected_bytes
    )


def _wrangler_failure_diagnostic(stdout: object, returncode: int) -> str:
    """Classify a narrow set of Wrangler JSON failures without reflecting data.

    Wrangler writes its ``--json`` fatal envelope to stdout.  Only the documented
    top-level error text is inspected, and the caller receives one of our fixed
    messages.  Unknown, malformed, or unexpectedly large output is deliberately
    suppressed rather than copied into logs.
    """
    text: str | None = None
    if isinstance(stdout, str) and len(stdout) <= 64 * 1024:
        try:
            value = json.loads(stdout)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict) and set(value) == {"error"}:
            error = value["error"]
            if isinstance(error, str):
                text = error
            elif isinstance(error, dict):
                candidate = error.get("text")
                if not isinstance(candidate, str):
                    candidate = error.get("message")
                if isinstance(candidate, str):
                    text = candidate

    if text is not None:
        folded = text.casefold()
        missing_or_expired_auth = (
            ("non-interactive" in folded and "cloudflare_api_token" in folded)
            or ("no credentials" in folded and "cloudflare_api_token" in folded)
            or "auth token has expired" in folded
        )
        rejected_auth = any(
            marker in folded
            for marker in (
                "authentication error",
                "invalid api token",
                "permission denied",
                "not authorized",
                "not permitted",
            )
        )
        timed_out = (
            "timed out" in folded
            or "timeout" in folded
            or "exceeded its cpu time limit" in folded
        )
        if missing_or_expired_auth:
            return (
                "wrangler authentication failed before the D1 query: set a "
                "D1 Read-scoped CLOUDFLARE_API_TOKEN; no bundle was published"
            )
        if rejected_auth:
            return (
                "wrangler authentication or authorization failed: verify a "
                "D1 Read-scoped CLOUDFLARE_API_TOKEN for the configured account "
                "and database; no bundle was published"
            )
        if timed_out:
            return "wrangler D1 query timed out; no bundle was published"

    return (
        f"wrangler d1 execute failed (exit status {returncode}; diagnostic output "
        "suppressed); no bundle was published"
    )


def run_wrangler(
    *,
    runner: Runner = subprocess.run,
    probe_runner: Runner = subprocess.run,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Run the reviewed query with a sealed config and sanitized environment."""
    _verify_isolated_startup()
    request = _request_bytes().decode("utf-8")
    _request_descriptor_bytes()
    environment = _sanitized_subprocess_environment()
    node, tool, toolchain = _pinned_toolchain(
        probe_runner=probe_runner, environment=environment
    )
    config_bytes = _minimal_wrangler_config()
    with tempfile.TemporaryDirectory(prefix="wikilean-d1-config-") as temporary:
        config_root = Path(temporary)
        config_root.chmod(0o700)
        config_path = config_root / "wrangler.json"
        stage_io.write_bytes_exclusive(config_path, config_bytes, mode=0o600)
        stage_io.fsync_directory(config_root)
        config_root_metadata = config_root.lstat()
        config_metadata = config_path.lstat()
        command = [
            str(node),
            "--no-warnings",
            str(WRANGLER_CLI),
            "d1",
            "execute",
            D1_BINDING,
            "--remote",
            "--json",
            "--config",
            str(config_path),
            "--command",
            request,
        ]
        result = runner(
            command,
            cwd=str(config_root),
            capture_output=True,
            text=True,
            env=dict(environment),
        )
        if not _same_regular_file(config_path, config_metadata, config_bytes):
            raise D1SnapshotError("private Wrangler config changed during execution")
        current_root = config_root.lstat()
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or stat.S_IMODE(current_root.st_mode) != 0o700
            or not os.path.samestat(config_root_metadata, current_root)
        ):
            raise D1SnapshotError("private Wrangler config directory changed during execution")
    if _file_sha256(node) != toolchain["node"]["sha256"]:
        raise D1SnapshotError("Node executable changed during acquisition")
    if _file_sha256(WRANGLER_CLI) != WRANGLER_CLI_SHA256:
        raise D1SnapshotError("Wrangler CLI changed during acquisition")
    _verify_runtime_closure(toolchain)
    if result.returncode != 0:
        raise D1SnapshotError(
            _wrangler_failure_diagnostic(result.stdout, result.returncode)
        )
    if not isinstance(result.stdout, str):
        raise D1SnapshotError("wrangler returned non-text stdout")
    return result.stdout, tool, toolchain


def _timestamp(value: str) -> str:
    candidate = value
    if not candidate.endswith("Z"):
        raise D1SnapshotError("audit timestamp must be RFC3339 UTC ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(candidate[:-1] + "+00:00")
    except ValueError as exc:
        raise D1SnapshotError("audit timestamp is not valid RFC3339 UTC") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise D1SnapshotError("audit timestamp must be UTC")
    return candidate


def _evidence_documents(
    outputs: Mapping[str, bytes],
    *,
    acquisition_tool: dict[str, Any],
    audit_time: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = outputs["acquired.jsonl"]
    raw_ref = _object_ref("d1_raw", raw, "application/x-ndjson")
    requests = [
        {
            "kind": "database_query",
            "uri": UPSTREAM_URI,
            "parameters_sha256": REQUEST_PARAMETERS_SHA256,
        }
    ]
    receipt: dict[str, Any] = {
        "schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1,
        "acquisition_receipt_id": "sha256:" + "0" * 64,
        "source": "wikilean-d1",
        "upstream_uri": UPSTREAM_URI,
        "pin": {"type": "content_sha256", "value": raw_ref["sha256"]},
        "tool": acquisition_tool,
        "requests": requests,
        "batch": {
            "status": "complete",
            "request_set_root": contracts.acquisition_request_set_root(requests),
            "requests_total": 1,
            "requests_succeeded": 1,
            "requests_failed": 0,
        },
        "outputs": [raw_ref],
        "audit": {"acquired_at": audit_time},
    }
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)

    normalized_refs = [
        _object_ref("articles", outputs["normalized/articles.jsonl"], "application/x-ndjson"),
        _object_ref(
            "brain_edges",
            outputs["normalized/brain_edges.jsonl"],
            "application/x-ndjson",
        ),
        _object_ref(
            "brain_nodes",
            outputs["normalized/brain_nodes.jsonl"],
            "application/x-ndjson",
        ),
        _object_ref("control", outputs["normalized/control.json"], "application/json"),
    ]
    lineage: dict[str, Any] = {
        "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
        "normalization_lineage_id": "sha256:" + "0" * 64,
        "source": "wikilean-d1",
        "mode": "transform",
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]],
        "parent_source_manifest_ids": [],
        "normalization_schema": NORMALIZATION_SCHEMA,
        "configuration_sha256": NORMALIZATION_CONFIGURATION_SHA256,
        "tool": {
            "name": "wikilean-d1-snapshot-normalizer",
            "version": "1",
            "sha256": LOADED_SCRIPT_SHA256,
        },
        "inputs": [
            {
                **raw_ref,
                "origin": {
                    "kind": "acquisition_receipt",
                    "id": receipt["acquisition_receipt_id"],
                },
            }
        ],
        "outputs": normalized_refs,
        "result": "complete",
        "audit": {"normalized_at": audit_time},
    }
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(
        lineage
    )
    contracts.validate_normalization_lineage(lineage)
    return receipt, lineage


def _bundle_files(
    canonical_rows: Sequence[dict[str, Any]],
    *,
    acquisition_tool: dict[str, Any],
    acquisition_toolchain: dict[str, Any],
    audit_time: str,
) -> tuple[str, dict[str, bytes]]:
    toolchain_bytes = contracts.canonical_json_bytes(acquisition_toolchain)
    if acquisition_tool.get("sha256") != _sha256(toolchain_bytes):
        raise D1SnapshotError("acquisition tool does not bind its toolchain closure")
    outputs = _normalization_outputs(canonical_rows)
    receipt, lineage = _evidence_documents(
        outputs, acquisition_tool=acquisition_tool, audit_time=_timestamp(audit_time)
    )
    bundle_id = lineage["normalization_lineage_id"]
    deterministic_members = [
        {
            "path": "request.sql",
            "sha256": SQL_SHA256,
            "bytes": len(_request_bytes()),
            "media_type": "application/sql",
        },
        {
            "path": "request.json",
            "sha256": REQUEST_PARAMETERS_SHA256,
            "bytes": len(_request_descriptor_bytes()),
            "media_type": "application/json",
        },
        {
            "path": "toolchain.json",
            "sha256": _sha256(toolchain_bytes),
            "bytes": len(toolchain_bytes),
            "media_type": "application/json",
        },
        *[
            {
                "path": path,
                "sha256": _sha256(data),
                "bytes": len(data),
                "media_type": (
                    "application/json" if path.endswith(".json") else "application/x-ndjson"
                ),
            }
            for path, data in sorted(outputs.items())
        ],
    ]
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "identity_basis": "normalization_lineage_id",
        "request_parameters_sha256": REQUEST_PARAMETERS_SHA256,
        "sql_sha256": SQL_SHA256,
        "toolchain_sha256": _sha256(toolchain_bytes),
        "acquisition_receipt_id": receipt["acquisition_receipt_id"],
        "normalization_lineage_id": lineage["normalization_lineage_id"],
        "members": deterministic_members,
        "evidence": {
            "acquisition_receipt": "acquisition-receipt.json",
            "normalization_lineage": "normalization-lineage.json",
        },
    }
    files = {
        "request.sql": _request_bytes(),
        "request.json": _request_descriptor_bytes(),
        "toolchain.json": toolchain_bytes,
        **outputs,
        "acquisition-receipt.json": contracts.canonical_json_bytes(receipt),
        "normalization-lineage.json": contracts.canonical_json_bytes(lineage),
        "bundle.json": contracts.canonical_json_bytes(manifest),
    }
    return bundle_id, files


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise D1SnapshotError(f"not a real directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise D1SnapshotError(f"directory must have mode 0700: {path}")


def _prepare_store(store: Path) -> tuple[Path, Path]:
    store = store.absolute()
    anchor = store.parent
    while not anchor.exists() and not anchor.is_symlink() and anchor != anchor.parent:
        anchor = anchor.parent
    anchor_metadata = anchor.lstat()
    if stat.S_ISLNK(anchor_metadata.st_mode) or not stat.S_ISDIR(anchor_metadata.st_mode):
        raise D1SnapshotError(f"bundle-store ancestor is not a real directory: {anchor}")
    if not store.exists() and not store.is_symlink():
        stage_io.ensure_private_directory(anchor, store)
    _require_private_directory(store)
    staging = store / ".staging"
    if not staging.exists() and not staging.is_symlink():
        stage_io.ensure_private_directory(store, staging)
    _require_private_directory(staging)
    return store, staging


def staging_orphans(store: Path) -> tuple[Path, ...]:
    """List private interrupted stages without guessing whether they are live."""
    staging = Path(store).absolute() / ".staging"
    if not staging.exists() and not staging.is_symlink():
        return ()
    _require_private_directory(staging)
    orphans: list[Path] = []
    for child in sorted(staging.iterdir(), key=lambda path: path.name):
        _require_private_directory(child)
        if not child.name.endswith(".tmp"):
            raise D1SnapshotError(f"unexpected entry in staging directory: {child}")
        orphans.append(child)
    return tuple(orphans)


def _write_stage(scratch: stage_io.OwnedDirectory, files: Mapping[str, bytes]) -> None:
    directories = sorted(
        {
            Path(path).parent.as_posix()
            for path in files
            if Path(path).parent.as_posix() != "."
        }
    )
    for relative in directories:
        directory = scratch.path / relative
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        directory.chmod(0o700)
        stage_io.fsync_directory(directory)
        stage_io.fsync_directory(directory.parent)
    for relative, data in sorted(files.items()):
        destination = scratch.path / relative
        stage_io.write_bytes_exclusive(destination, data, mode=0o644)
    stage_io.fsync_directory(scratch.path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        value = contracts.parse_json_bytes(data, location=str(path))
    except (OSError, contracts.VerificationError) as exc:
        raise D1SnapshotError(str(exc)) from exc
    if not isinstance(value, dict):
        raise D1SnapshotError(f"{path}: expected object")
    if data != contracts.canonical_json_bytes(value):
        raise D1SnapshotError(f"{path}: evidence document is not canonical JSON")
    return value


def _verify_published(
    target: Path,
    bundle_id: str,
    expected_files: Mapping[str, bytes],
) -> None:
    _require_private_directory(target)
    actual_paths: set[str] = set()
    for directory, names, filenames in os.walk(target, followlinks=False):
        directory_path = Path(directory)
        _require_private_directory(directory_path)
        names.sort()
        filenames.sort()
        for name in names:
            child = directory_path / name
            _require_private_directory(child)
        for name in filenames:
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise D1SnapshotError(f"published bundle has non-file member: {child}")
            if stat.S_IMODE(metadata.st_mode) != 0o644:
                raise D1SnapshotError(f"published bundle file mode is not 0644: {child}")
            actual_paths.add(child.relative_to(target).as_posix())
    if actual_paths != set(expected_files):
        raise D1SnapshotError("published bundle member set does not match candidate")

    # Logical evidence IDs deliberately ignore audit timestamps.  Every other
    # member is byte-for-byte content addressed; evidence documents are checked
    # by the normative validators and must resolve to the same IDs.
    for relative, expected in expected_files.items():
        if relative in {"acquisition-receipt.json", "normalization-lineage.json"}:
            continue
        actual = (target / relative).read_bytes()
        if actual != expected:
            raise D1SnapshotError(f"published bundle member mismatch: {relative}")
    receipt = _read_json(target / "acquisition-receipt.json")
    lineage = _read_json(target / "normalization-lineage.json")
    try:
        contracts.validate_acquisition_receipt(receipt)
        contracts.validate_normalization_lineage(lineage)
    except contracts.VerificationError as exc:
        raise D1SnapshotError(str(exc)) from exc
    if lineage["normalization_lineage_id"] != bundle_id:
        raise D1SnapshotError("published bundle lineage does not match directory identity")
    expected_receipt = _read_json_bytes(expected_files["acquisition-receipt.json"])
    if receipt["acquisition_receipt_id"] != expected_receipt["acquisition_receipt_id"]:
        raise D1SnapshotError("published bundle acquisition receipt identity mismatch")
    if lineage["acquisition_receipt_ids"] != [receipt["acquisition_receipt_id"]]:
        raise D1SnapshotError("published bundle lineage does not bind its receipt")


def _read_json_bytes(data: bytes) -> dict[str, Any]:
    try:
        value = contracts.parse_json_bytes(data, location="candidate evidence")
    except contracts.VerificationError as exc:
        raise D1SnapshotError(str(exc)) from exc
    if not isinstance(value, dict):
        raise D1SnapshotError("candidate evidence: expected object")
    return value


def publish_response(
    stdout: str,
    *,
    store: Path,
    acquisition_tool: dict[str, Any],
    acquisition_toolchain: dict[str, Any],
    audit_time: str,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    """Validate one response and atomically publish its complete sealed bundle."""
    _verify_runtime_closure(acquisition_toolchain)
    canonical_rows = parse_wrangler_output(stdout)
    bundle_id, files = _bundle_files(
        canonical_rows,
        acquisition_tool=acquisition_tool,
        acquisition_toolchain=acquisition_toolchain,
        audit_time=audit_time,
    )
    store, staging = _prepare_store(Path(store))
    target = store / bundle_id.removeprefix("sha256:")
    scratch_path = staging / (
        bundle_id.removeprefix("sha256:") + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    published = False
    try:
        with stage_io.owned_directory(store, scratch_path) as scratch:
            _write_stage(scratch, files)
            if before_publish is not None:
                before_publish(scratch.path, target)
            _verify_runtime_closure(acquisition_toolchain)
            try:
                stage_io.publish_directory_no_replace(scratch, target)
                published = True
            except FileExistsError:
                # A concurrent publisher may have won.  The context removes our
                # still-owned scratch; the winner is accepted only after a full
                # logical and byte-level verification below.
                pass
    finally:
        if published:
            stage_io.fsync_directory(store)
    _verify_published(target, bundle_id, files)
    return target


def acquire_snapshot(
    *,
    store: Path,
    audit_time: str,
    runner: Runner = subprocess.run,
    probe_runner: Runner = subprocess.run,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    _verify_isolated_startup()
    _verify_loaded_program()
    stdout, tool, toolchain = run_wrangler(
        runner=runner, probe_runner=probe_runner
    )
    return publish_response(
        stdout,
        store=store,
        acquisition_tool=tool,
        acquisition_toolchain=toolchain,
        audit_time=audit_time,
        before_publish=before_publish,
    )


def _now_utc() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _verify_isolated_startup()
    except D1SnapshotError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="private content-addressed bundle store",
    )
    parser.add_argument(
        "--audit-time",
        default=_now_utc(),
        help="RFC3339 UTC audit timestamp (excluded from logical identities)",
    )
    args = parser.parse_args(argv)
    try:
        target = acquire_snapshot(store=args.store, audit_time=args.audit_time)
    except (D1SnapshotError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    orphans = staging_orphans(args.store)
    if orphans:
        print(
            f"WARNING: {len(orphans)} private interrupted stage(s) remain in "
            f"{Path(args.store).absolute() / '.staging'}; remove them only when no "
            "acquisition process is active",
            file=sys.stderr,
        )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
