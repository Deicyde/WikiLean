#!/usr/bin/env python3
"""Independently verify and load a sealed Wikidata entity bundle.

This module performs no acquisition and does not import the acquisition
wrapper.  It independently checks the exact request plan and request
preimages, the complete response/bisection transcript, the pinned toolchain,
the authority receipt/lineage chain, and the clock-free normalized entity map.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402

REQUEST_PLAN_SCHEMA = "wikilean.wikidata-entity-request-plan/v1"
REQUESTED_QID_SET_DOMAIN = "wikilean.wikidata-requested-qid-set.v1"
BUNDLE_SCHEMA = "wikilean.wikidata-entity-acquisition-bundle/v1"
NORMALIZATION_SCHEMA = "wikilean.wikidata-entity-map/v1"
TOOLCHAIN_SCHEMA = "wikilean.wikidata-entity-acquisition-toolchain/v1"
UPSTREAM_URI = "https://www.wikidata.org/w/api.php"
USER_AGENT = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"
BATCH_SIZE = 50
MAX_PLAN_QIDS = 50_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")
PYTHON_VERSION_RE = re.compile(r"^3\.12\.[0-9]+(?:[+.-][A-Za-z0-9.+-]+)?$")
REQUEST_NAME_RE = re.compile(r"^[0-9]{6}\.form$")
REQUEST_MEDIA_TYPE = "application/x-www-form-urlencoded"
NORMALIZED_PATH = "normalized/entities.json"

REQUEST_FIELDS = (
    ("action", "wbgetentities"),
    ("format", "json"),
    ("formatversion", "2"),
    ("maxlag", "5"),
    ("props", "labels|descriptions|aliases|claims|sitelinks|info"),
    ("languages", "en"),
    ("sitefilter", "enwiki"),
    ("redirects", "yes"),
)
CURL_ARGUMENTS = [
    "--disable",
    "--silent",
    "--show-error",
    "--fail-with-body",
    "--proto",
    "=https",
    "--tlsv1.2",
    "--connect-timeout",
    "30",
    "--max-time",
    "90",
    "--max-filesize",
    str(MAX_RESPONSE_BYTES),
    "--request",
    "POST",
    "--header",
    "Accept: application/json",
    "--header",
    f"Content-Type: {REQUEST_MEDIA_TYPE}",
    "--header",
    f"User-Agent: {USER_AGENT}",
    "--data-binary",
    "@-",
    "--write-out",
    "%{stderr}wikilean-http-v1\\t%{http_code}\\t%{content_type}\\n",
]
FORWARDED_ENVIRONMENT: list[str] = []
FORCED_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "NO_PROXY": "*",
    "PATH": "/usr/bin:/bin",
}
HTTP_RESPONSE_POLICY = {
    "status": 200,
    "content_type": "application/json",
    "retry": "none-fail-closed",
}
REQUIRED_PYTHON_STARTUP_FLAGS = {
    "ignore_environment": True,
    "isolated": True,
    "no_site": True,
    "no_user_site": True,
    "safe_path": True,
}
LOCAL_DEPENDENCY_PINS = (
    {
        "path": "brain/stage_io.py",
        "sha256": "9b659899ce6c62709ac75b8bec2b9d83cd8550281e5d0ca2122ea6a8a805e4cf",
    },
    {
        "path": "brain/tools/authority_contracts.py",
        "sha256": "fb2f105b2cad2a5ceed38925694f8da1766b57774a7e730078f537b68da018c6",
    },
)
ACQUIRER_WRAPPER_SHA256 = (
    "9a4d3b87ee1ebd85d46003d221f8ee3c6eef312961c283cc6feed6fb3c37651e"
)

NORMALIZATION_CONFIGURATION = {
    "schema": NORMALIZATION_SCHEMA,
    "request_plan_schema": REQUEST_PLAN_SCHEMA,
    "batch_size": BATCH_SIZE,
    "request_fields": [list(item) for item in REQUEST_FIELDS],
    "redirect_policy": "resolve-complete-acyclic-chains/v1",
    "missing_policy": "explicit-marker-or-isolated-no-such-entity/v1",
    "entity_fields": [
        "qid",
        "requested",
        "label",
        "aliases",
        "description",
        "classes",
        "enwiki_slug",
        "lastrevid",
        "modified",
    ],
    "collection_policy": "unicode-nfc-sorted-unique/v1",
}
NORMALIZATION_CONFIGURATION_SHA256 = hashlib.sha256(
    contracts.canonical_json_bytes(NORMALIZATION_CONFIGURATION)
).hexdigest()

ROOT_ENTRIES = frozenset({
    "request-plan.json",
    "toolchain.json",
    "acquired.jsonl",
    "acquisition-receipt.json",
    "normalization-lineage.json",
    "bundle.json",
    "normalized",
    "requests",
})


class WikidataEntityBundleError(RuntimeError):
    """A sealed entity bundle is incomplete, altered, or untrusted."""


@dataclass(frozen=True)
class WikidataEntityBundle:
    path: Path
    acquisition_receipt_id: str
    normalization_lineage_id: str
    acquired_at: str
    requested_qids: tuple[str, ...]
    entities: dict[str, dict[str, Any]]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_object(
    value: Any, fields: Sequence[str], location: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WikidataEntityBundleError(f"{location}: expected object")
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        raise WikidataEntityBundleError(
            f"{location}: wrong fields "
            f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )
    return value


def _request_body(qids: Sequence[str]) -> bytes:
    return urllib.parse.urlencode(
        [*REQUEST_FIELDS, ("ids", "|".join(qids))],
        doseq=False,
        safe="",
        quote_via=urllib.parse.quote,
    ).encode("ascii")


def requested_qid_set_root(qids: Sequence[str]) -> str:
    return contracts.domain_hash(REQUESTED_QID_SET_DOMAIN, {"qids": list(qids)})


def _entity_summary(entities: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    missing = sum(item.get("missing") is True for item in entities.values())
    redirected = sum(
        item.get("missing") is not True and item.get("qid") != requested
        for requested, item in entities.items()
    )
    return {
        "requested": len(entities),
        "direct": len(entities) - missing - redirected,
        "redirected": redirected,
        "missing": missing,
    }


def _validate_request_plan(data: bytes) -> dict[str, Any]:
    try:
        value = contracts.parse_json_bytes(data, location="request-plan.json")
    except contracts.VerificationError as exc:
        raise WikidataEntityBundleError(str(exc)) from exc
    plan = _exact_object(value, ("schema", "qids"), "request-plan.json")
    if plan["schema"] != REQUEST_PLAN_SCHEMA:
        raise WikidataEntityBundleError("request-plan.json.schema: unexpected schema")
    qids = plan["qids"]
    if not isinstance(qids, list) or not qids or len(qids) > MAX_PLAN_QIDS:
        raise WikidataEntityBundleError("request-plan.json.qids: invalid plan size")
    for index, qid in enumerate(qids):
        if not isinstance(qid, str) or QID_RE.fullmatch(qid) is None:
            raise WikidataEntityBundleError(
                f"request-plan.json.qids[{index}]: expected canonical QID"
            )
    if qids != sorted(set(qids)):
        raise WikidataEntityBundleError(
            "request-plan.json.qids: entries are not unique and sorted"
        )
    if data != contracts.canonical_json_bytes(plan):
        raise WikidataEntityBundleError("request-plan.json: expected canonical JSON bytes")
    return plan


def _strict_response_json(data: bytes, location: str) -> Any:
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise WikidataEntityBundleError(f"{location}: invalid response size")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise WikidataEntityBundleError(
                    f"{location}: duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise WikidataEntityBundleError(
            f"{location}: non-finite JSON number {value!r}"
        )

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except WikidataEntityBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikidataEntityBundleError(f"{location}: invalid JSON") from exc


def _nfc_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or "\x00" in value \
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise WikidataEntityBundleError(f"{location}: expected a valid string")
    return unicodedata.normalize("NFC", value)


def _utc_timestamp(value: str, location: str) -> str:
    if contracts.UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise WikidataEntityBundleError(f"{location}: expected RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WikidataEntityBundleError(f"{location}: invalid RFC3339 timestamp") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise WikidataEntityBundleError(f"{location}: expected UTC timestamp")
    return value


def _no_such_error_matches(error: Mapping[str, Any], qid: str) -> bool:
    cited: set[str] = set()
    info = error.get("info")
    if isinstance(info, str):
        cited.update(re.findall(r"(?<![A-Za-z0-9])Q[1-9][0-9]{0,11}(?![0-9])", info))
    messages = error.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            parameters = message.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if isinstance(parameter, str) and QID_RE.fullmatch(parameter):
                    cited.add(parameter)
    return cited == {qid}


def _normalize_success_payload(
    payload: Any,
    requested_qids: Sequence[str],
    *,
    location: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) \
            or set(payload) - {"success", "entities", "redirects"}:
        raise WikidataEntityBundleError(f"{location}: unexpected response shape")
    if type(payload.get("success")) is not int or payload["success"] != 1:
        raise WikidataEntityBundleError(
            f"{location}: expected exact formatversion=2 success marker"
        )
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise WikidataEntityBundleError(f"{location}: missing entities object")
    if any(not isinstance(key, str) or QID_RE.fullmatch(key) is None for key in entities):
        raise WikidataEntityBundleError(f"{location}: malformed entity key")
    rows = payload.get("redirects", [])
    if not isinstance(rows, list):
        raise WikidataEntityBundleError(f"{location}: redirects is not an array")
    redirects: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"from", "to"} \
                or not isinstance(row.get("from"), str) \
                or not isinstance(row.get("to"), str) \
                or QID_RE.fullmatch(row["from"]) is None \
                or QID_RE.fullmatch(row["to"]) is None \
                or row["from"] in redirects:
            raise WikidataEntityBundleError(
                f"{location}.redirects[{index}]: malformed or duplicate redirect"
            )
        redirects[row["from"]] = row["to"]

    output: dict[str, dict[str, Any]] = {}
    used_redirects: set[str] = set()
    used_entity_keys: set[str] = set()
    for requested in requested_qids:
        top_resolved = requested
        seen: set[str] = set()
        while top_resolved in redirects:
            if top_resolved in seen:
                raise WikidataEntityBundleError(f"{location}: redirect cycle")
            seen.add(top_resolved)
            used_redirects.add(top_resolved)
            top_resolved = redirects[top_resolved]
        available = [key for key in (requested, top_resolved) if key in entities]
        if not available:
            raise WikidataEntityBundleError(
                f"{location}: omitted requested QID {requested}"
            )
        if len(set(available)) != 1:
            raise WikidataEntityBundleError(
                f"{location}: ambiguous entity rows for {requested}"
            )
        entity_key = available[0]
        used_entity_keys.add(entity_key)
        entity = entities[entity_key]
        if not isinstance(entity, dict):
            raise WikidataEntityBundleError(f"{location}: entity is not an object")
        if "missing" in entity:
            if entity != {"id": requested, "missing": ""} \
                    or top_resolved != requested:
                raise WikidataEntityBundleError(
                    f"{location}: entity has invalid missing row"
                )
            output[requested] = {"missing": True}
            continue
        if "id" not in entity:
            raise WikidataEntityBundleError(f"{location}: entity lacks an explicit id")
        entity_id = entity.get("id", top_resolved)
        if not isinstance(entity_id, str) or QID_RE.fullmatch(entity_id) is None:
            raise WikidataEntityBundleError(f"{location}: malformed entity id")
        local_redirect = entity.get("redirects")
        if entity_id != requested:
            if local_redirect != {"from": requested, "to": entity_id}:
                raise WikidataEntityBundleError(
                    f"{location}: redirected entity lacks exact entity-local binding"
                )
        elif local_redirect is not None:
            raise WikidataEntityBundleError(
                f"{location}: non-redirected entity has redirect metadata"
            )
        if top_resolved != requested and entity_id != top_resolved:
            raise WikidataEntityBundleError(
                f"{location}: top-level and entity-local redirects conflict"
            )
        containers: dict[str, dict[str, Any]] = {}
        for field in ("labels", "aliases", "descriptions", "claims", "sitelinks"):
            item = entity.get(field, {})
            if not isinstance(item, dict):
                raise WikidataEntityBundleError(
                    f"{location}: entity has malformed {field}"
                )
            containers[field] = item
        label_row = containers["labels"].get("en")
        if label_row is not None and (
            not isinstance(label_row, dict) or "value" not in label_row
        ):
            raise WikidataEntityBundleError(f"{location}: malformed English label")
        label = None if label_row is None else _nfc_string(
            label_row["value"], f"{location}.label"
        )
        alias_rows = containers["aliases"].get("en", [])
        if not isinstance(alias_rows, list):
            raise WikidataEntityBundleError(f"{location}: malformed English aliases")
        aliases: list[str] = []
        for index, row in enumerate(alias_rows):
            if not isinstance(row, dict) or "value" not in row:
                raise WikidataEntityBundleError(f"{location}: malformed alias {index}")
            aliases.append(_nfc_string(row["value"], f"{location}.alias[{index}]"))
        description_row = containers["descriptions"].get("en")
        if description_row is not None and (
            not isinstance(description_row, dict) or "value" not in description_row
        ):
            raise WikidataEntityBundleError(f"{location}: malformed description")
        description = None if description_row is None else _nfc_string(
            description_row["value"], f"{location}.description"
        )
        p31_rows = containers["claims"].get("P31", [])
        if not isinstance(p31_rows, list):
            raise WikidataEntityBundleError(f"{location}: malformed P31 claims")
        classes: list[str] = []
        for index, claim in enumerate(p31_rows):
            if not isinstance(claim, dict) or not isinstance(claim.get("mainsnak"), dict):
                raise WikidataEntityBundleError(f"{location}: malformed P31 claim {index}")
            datavalue = claim["mainsnak"].get("datavalue")
            if datavalue is None:
                continue
            if not isinstance(datavalue, dict) or not isinstance(datavalue.get("value"), dict):
                raise WikidataEntityBundleError(f"{location}: malformed P31 value {index}")
            class_qid = datavalue["value"].get("id")
            if not isinstance(class_qid, str) or QID_RE.fullmatch(class_qid) is None:
                raise WikidataEntityBundleError(f"{location}: malformed P31 value {index}")
            classes.append(class_qid)
        enwiki = containers["sitelinks"].get("enwiki")
        if enwiki is not None and (
            not isinstance(enwiki, dict) or "title" not in enwiki
        ):
            raise WikidataEntityBundleError(f"{location}: malformed enwiki sitelink")
        title = None if enwiki is None else _nfc_string(
            enwiki["title"], f"{location}.enwiki"
        )
        lastrevid = entity.get("lastrevid")
        if type(lastrevid) is not int or not 0 <= lastrevid <= contracts.MAX_SAFE_INTEGER:
            raise WikidataEntityBundleError(f"{location}: malformed lastrevid")
        modified = _nfc_string(entity.get("modified"), f"{location}.modified")
        _utc_timestamp(modified, f"{location}.modified")
        output[requested] = {
            "qid": entity_id,
            "requested": requested,
            "label": label,
            "aliases": sorted(set(aliases)),
            "description": description,
            "classes": sorted(set(classes)),
            "enwiki_slug": title.replace(" ", "_") if title is not None else None,
            "lastrevid": lastrevid,
            "modified": modified,
        }
    if set(redirects) != used_redirects:
        raise WikidataEntityBundleError(f"{location}: unreferenced redirect row")
    if set(entities) != used_entity_keys:
        raise WikidataEntityBundleError(f"{location}: unreferenced entity row")
    return output


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_at(
    directory_fd: int, name: str, display: str, *, maximum: int | None = None
) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o644 \
                    or before.st_nlink != 1:
                raise WikidataEntityBundleError(
                    f"bundle member is not a mode-0644 regular file: {display}"
                )
            if maximum is not None and before.st_size > maximum:
                raise WikidataEntityBundleError(f"bundle member is too large: {display}")
            chunks: list[bytes] = []
            while block := os.read(descriptor, 1024 * 1024):
                chunks.append(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except WikidataEntityBundleError:
        raise
    except OSError as exc:
        raise WikidataEntityBundleError(f"cannot read bundle member {display}: {exc}") from exc
    if not stat.S_ISREG(after.st_mode) or not stat.S_ISREG(linked.st_mode) \
            or after.st_nlink != 1 or linked.st_nlink != 1 \
            or _file_state(before) != _file_state(after) \
            or not os.path.samestat(after, linked) \
            or after.st_size != sum(map(len, chunks)):
        raise WikidataEntityBundleError(f"bundle member changed while read: {display}")
    return b"".join(chunks)


def _open_directory_at(parent_fd: int | None, name: str | Path, display: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if parent_fd is None:
            descriptor = os.open(name, flags)
        else:
            descriptor = os.open(str(name), flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise WikidataEntityBundleError(f"cannot open bundle directory {display}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise WikidataEntityBundleError(f"bundle directory is not a mode-0700 directory: {display}")
    return descriptor, metadata


def _bundle_bytes(bundle_path: Path) -> dict[str, bytes]:
    bundle_path = Path(bundle_path).absolute()
    root_fd = normalized_fd = requests_fd = -1
    try:
        path_before = bundle_path.lstat()
        root_fd, root_meta = _open_directory_at(None, bundle_path, str(bundle_path))
        if stat.S_ISLNK(path_before.st_mode) or not os.path.samestat(path_before, root_meta):
            raise WikidataEntityBundleError("bundle path is not a stable real directory")
        if frozenset(os.listdir(root_fd)) != ROOT_ENTRIES:
            raise WikidataEntityBundleError("bundle root member closure mismatch")
        normalized_fd, normalized_meta = _open_directory_at(root_fd, "normalized", "normalized")
        requests_fd, requests_meta = _open_directory_at(root_fd, "requests", "requests")
        if frozenset(os.listdir(normalized_fd)) != {"entities.json"}:
            raise WikidataEntityBundleError("normalized member closure mismatch")
        request_names = sorted(os.listdir(requests_fd))
        if not request_names or len(request_names) > contracts.MAX_ACQUISITION_REQUESTS \
                or any(REQUEST_NAME_RE.fullmatch(name) is None for name in request_names):
            raise WikidataEntityBundleError("request preimage member closure mismatch")
        files = {
            name: _read_regular_at(root_fd, name, name)
            for name in (
                "request-plan.json",
                "toolchain.json",
                "acquired.jsonl",
                "acquisition-receipt.json",
                "normalization-lineage.json",
                "bundle.json",
            )
        }
        files[NORMALIZED_PATH] = _read_regular_at(
            normalized_fd, "entities.json", NORMALIZED_PATH
        )
        for name in request_names:
            files[f"requests/{name}"] = _read_regular_at(
                requests_fd, name, f"requests/{name}", maximum=16 * 1024
            )
        if frozenset(os.listdir(root_fd)) != ROOT_ENTRIES \
                or frozenset(os.listdir(normalized_fd)) != {"entities.json"} \
                or sorted(os.listdir(requests_fd)) != request_names:
            raise WikidataEntityBundleError("bundle closure changed while read")
        for name, metadata in (("normalized", normalized_meta), ("requests", requests_meta)):
            linked = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not os.path.samestat(metadata, linked):
                raise WikidataEntityBundleError(f"bundle {name} directory changed while read")
        path_after = bundle_path.lstat()
        if stat.S_ISLNK(path_after.st_mode) or not os.path.samestat(root_meta, path_after):
            raise WikidataEntityBundleError("bundle directory changed while read")
        return files
    except OSError as exc:
        raise WikidataEntityBundleError(f"cannot read entity bundle: {exc}") from exc
    finally:
        for descriptor in (requests_fd, normalized_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _canonical_json(data: bytes, location: str) -> dict[str, Any]:
    try:
        value = contracts.parse_json_bytes(data, location=location)
    except contracts.VerificationError as exc:
        raise WikidataEntityBundleError(str(exc)) from exc
    if not isinstance(value, dict) or data != contracts.canonical_json_bytes(value):
        raise WikidataEntityBundleError(f"{location}: expected canonical JSON object")
    return value


def _artifact_json(data: bytes, location: str) -> Any:
    try:
        value = contracts.parse_artifact_json_bytes(data, location=location)
    except contracts.VerificationError as exc:
        raise WikidataEntityBundleError(str(exc)) from exc
    if data != contracts.canonical_artifact_json_bytes(value):
        raise WikidataEntityBundleError(f"{location}: expected canonical artifact JSON")
    return value


def _validate_toolchain(value: Any) -> dict[str, Any]:
    toolchain = _exact_object(
        value,
        ("schema", "invocation", "curl", "python", "local_dependencies", "wrapper"),
        "toolchain.json",
    )
    if toolchain["schema"] != TOOLCHAIN_SCHEMA:
        raise WikidataEntityBundleError("toolchain.json.schema: unexpected schema")
    invocation = _exact_object(
        toolchain["invocation"],
        (
            "uri", "arguments", "forwarded_environment", "forced_environment",
            "response_policy",
        ),
        "toolchain.json.invocation",
    )
    if invocation != {
        "uri": UPSTREAM_URI,
        "arguments": CURL_ARGUMENTS,
        "forwarded_environment": FORWARDED_ENVIRONMENT,
        "forced_environment": FORCED_ENVIRONMENT,
        "response_policy": HTTP_RESPONSE_POLICY,
    }:
        raise WikidataEntityBundleError("toolchain.json.invocation: unexpected policy")
    curl = _exact_object(toolchain["curl"], ("version", "sha256"), "toolchain.json.curl")
    if not isinstance(curl["version"], str) or not curl["version"].startswith("curl ") \
            or len(curl["version"]) > 512 \
            or not isinstance(curl["sha256"], str) \
            or contracts.DIGEST_RE.fullmatch(curl["sha256"]) is None:
        raise WikidataEntityBundleError("toolchain.json.curl: invalid exact runtime pin")
    python = _exact_object(
        toolchain["python"],
        ("implementation", "version", "sha256", "startup_flags"),
        "toolchain.json.python",
    )
    if python["implementation"] != "CPython" \
            or not isinstance(python["version"], str) \
            or PYTHON_VERSION_RE.fullmatch(python["version"]) is None \
            or not isinstance(python["sha256"], str) \
            or contracts.DIGEST_RE.fullmatch(python["sha256"]) is None \
            or python["startup_flags"] != REQUIRED_PYTHON_STARTUP_FLAGS:
        raise WikidataEntityBundleError("toolchain.json.python: invalid exact runtime pin")
    if toolchain["local_dependencies"] != list(LOCAL_DEPENDENCY_PINS):
        raise WikidataEntityBundleError("toolchain.json.local_dependencies: pin mismatch")
    wrapper = _exact_object(toolchain["wrapper"], ("sha256",), "toolchain.json.wrapper")
    if wrapper["sha256"] != ACQUIRER_WRAPPER_SHA256:
        raise WikidataEntityBundleError("toolchain.json.wrapper.sha256: pin mismatch")
    return toolchain


def _object_ref(name: str, data: bytes, media_type: str) -> dict[str, Any]:
    return {"object": name, "sha256": _sha256(data), "bytes": len(data), "media_type": media_type}


def _read_transcript(
    raw: bytes, request_files: Mapping[str, bytes]
) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise WikidataEntityBundleError("acquired.jsonl: missing content or final newline")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines()):
        if not line:
            raise WikidataEntityBundleError(f"acquired.jsonl:{index + 1}: blank line")
        record = _exact_object(
            _artifact_json(line, f"acquired.jsonl:{index + 1}"),
            (
                "request_index", "qids", "request_sha256", "http_status",
                "content_type", "response_base64",
            ),
            f"acquired.jsonl:{index + 1}",
        )
        if record["request_index"] != index:
            raise WikidataEntityBundleError("acquired.jsonl: request indices are reordered")
        qids = record["qids"]
        if not isinstance(qids, list) or not qids or len(qids) > BATCH_SIZE \
                or qids != sorted(set(qids)) \
                or any(not isinstance(qid, str) or QID_RE.fullmatch(qid) is None for qid in qids):
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}.qids: invalid canonical batch"
            )
        body = _request_body(qids)
        path = f"requests/{index:06d}.form"
        if request_files.get(path) != body:
            raise WikidataEntityBundleError(f"{path}: not the exact request preimage")
        if record["request_sha256"] != _sha256(body):
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}: request digest mismatch"
            )
        if record["http_status"] != HTTP_RESPONSE_POLICY["status"]:
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}: unexpected HTTP status"
            )
        if not isinstance(record["content_type"], str) \
                or record["content_type"].split(";", 1)[0].strip().casefold() \
                != HTTP_RESPONSE_POLICY["content_type"]:
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}: unexpected content type"
            )
        if not isinstance(record["response_base64"], str):
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}: response is not base64 text"
            )
        try:
            response = base64.b64decode(record["response_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{index + 1}: invalid response base64"
            ) from exc
        record = dict(record)
        record["response"] = response
        records.append(record)
    expected_paths = {f"requests/{index:06d}.form" for index in range(len(records))}
    if set(request_files) != expected_paths:
        raise WikidataEntityBundleError("request preimage path closure mismatch")
    return records


def _replay_transcript(
    plan: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    cursor = 0
    entities: dict[str, dict[str, Any]] = {}

    def consume(qids: tuple[str, ...]) -> None:
        nonlocal cursor
        if cursor >= len(records):
            raise WikidataEntityBundleError("acquired.jsonl: truncated bisection transcript")
        record = records[cursor]
        request_index = cursor
        cursor += 1
        if record["qids"] != list(qids):
            raise WikidataEntityBundleError(
                f"acquired.jsonl:{request_index + 1}: request order/bisection mismatch"
            )
        payload = _strict_response_json(
            record["response"], f"acquired.jsonl:{request_index + 1}.response"
        )
        if isinstance(payload, dict) and "error" in payload:
            if set(payload) not in ({"error"}, {"error", "servedby"}) \
                    or not isinstance(payload["error"], dict) \
                    or not isinstance(payload["error"].get("code"), str) \
                    or ("servedby" in payload and not isinstance(payload["servedby"], str)):
                raise WikidataEntityBundleError("acquired.jsonl: malformed API error")
            if payload["error"]["code"] != "no-such-entity":
                raise WikidataEntityBundleError("acquired.jsonl: non-isolatable API error")
            if len(qids) == 1:
                if not _no_such_error_matches(payload["error"], qids[0]):
                    raise WikidataEntityBundleError(
                        "acquired.jsonl: singleton no-such-entity QID mismatch"
                    )
                entities[qids[0]] = {"missing": True}
                return
            midpoint = len(qids) // 2
            consume(qids[:midpoint])
            consume(qids[midpoint:])
            return
        normalized = _normalize_success_payload(
            payload, qids, location=f"acquired.jsonl:{request_index + 1}.response"
        )
        if set(normalized) & set(entities):
            raise WikidataEntityBundleError("acquired.jsonl: duplicate normalized QID")
        entities.update(normalized)

    qids = plan["qids"]
    for offset in range(0, len(qids), BATCH_SIZE):
        consume(tuple(qids[offset:offset + BATCH_SIZE]))
    if cursor != len(records):
        raise WikidataEntityBundleError("acquired.jsonl: unreferenced response records")
    if sorted(entities) != qids:
        raise WikidataEntityBundleError("acquired.jsonl: incomplete request-plan coverage")
    return entities


def verify_wikidata_entity_bundle(bundle_path: Path) -> WikidataEntityBundle:
    """Load and independently verify one complete entity acquisition bundle."""
    bundle_path = Path(bundle_path).absolute()
    files = _bundle_bytes(bundle_path)
    plan = _validate_request_plan(files["request-plan.json"])
    manifest = _canonical_json(files["bundle.json"], "bundle.json")
    receipt = _canonical_json(files["acquisition-receipt.json"], "acquisition-receipt.json")
    lineage = _canonical_json(files["normalization-lineage.json"], "normalization-lineage.json")
    toolchain = _validate_toolchain(_canonical_json(files["toolchain.json"], "toolchain.json"))
    try:
        contracts.validate_acquisition_receipt(receipt, location="acquisition-receipt.json")
        contracts.validate_normalization_lineage(lineage, location="normalization-lineage.json")
    except contracts.VerificationError as exc:
        raise WikidataEntityBundleError(str(exc)) from exc

    _exact_object(
        manifest,
        (
            "schema", "bundle_id", "identity_basis", "request_plan_sha256",
            "requested_qid_set_root", "summary", "toolchain_sha256", "acquisition_receipt_id",
            "normalization_lineage_id", "members", "evidence",
        ),
        "bundle.json",
    )
    if manifest["schema"] != BUNDLE_SCHEMA \
            or manifest["identity_basis"] != "normalization_lineage_id":
        raise WikidataEntityBundleError("bundle.json: unexpected schema or identity basis")
    receipt_id = receipt["acquisition_receipt_id"]
    lineage_id = lineage["normalization_lineage_id"]
    if manifest["bundle_id"] != lineage_id \
            or manifest["normalization_lineage_id"] != lineage_id \
            or manifest["acquisition_receipt_id"] != receipt_id \
            or bundle_path.name != lineage_id.removeprefix("sha256:"):
        raise WikidataEntityBundleError("bundle identity closure mismatch")
    if manifest["evidence"] != {
        "acquisition_receipt": "acquisition-receipt.json",
        "normalization_lineage": "normalization-lineage.json",
    }:
        raise WikidataEntityBundleError("bundle.json.evidence: unexpected paths")
    if manifest["request_plan_sha256"] != _sha256(files["request-plan.json"]) \
            or manifest["toolchain_sha256"] != _sha256(files["toolchain.json"]):
        raise WikidataEntityBundleError("bundle manifest preimage/toolchain digest mismatch")
    if manifest["requested_qid_set_root"] != requested_qid_set_root(plan["qids"]):
        raise WikidataEntityBundleError("bundle requested-QID-set root mismatch")

    deterministic_paths = sorted(
        set(files) - {"bundle.json", "acquisition-receipt.json", "normalization-lineage.json"}
    )
    members = manifest["members"]
    if not isinstance(members, list) or len(members) != len(deterministic_paths):
        raise WikidataEntityBundleError("bundle.json.members: unexpected member count")
    for index, (item, path) in enumerate(zip(members, deterministic_paths, strict=True)):
        entry = _exact_object(
            item, ("path", "sha256", "bytes", "media_type"),
            f"bundle.json.members[{index}]",
        )
        if path.startswith("requests/"):
            media_type = REQUEST_MEDIA_TYPE
        elif path == "acquired.jsonl":
            media_type = "application/x-ndjson"
        else:
            media_type = "application/json"
        expected = {
            "path": path,
            "sha256": _sha256(files[path]),
            "bytes": len(files[path]),
            "media_type": media_type,
        }
        if entry != expected:
            raise WikidataEntityBundleError(
                f"bundle.json.members[{index}]: member binding mismatch"
            )

    request_files = {
        path: data for path, data in files.items() if path.startswith("requests/")
    }
    records = _read_transcript(files["acquired.jsonl"], request_files)
    entities = _replay_transcript(plan, records)
    normalized = {
        "schema": NORMALIZATION_SCHEMA,
        "entities": entities,
    }
    if files[NORMALIZED_PATH] != contracts.canonical_artifact_json_bytes(normalized):
        raise WikidataEntityBundleError(
            f"{NORMALIZED_PATH}: does not exactly normalize acquired.jsonl"
        )
    if manifest["summary"] != _entity_summary(entities):
        raise WikidataEntityBundleError("bundle summary does not match normalized entities")

    toolchain_sha = _sha256(files["toolchain.json"])
    raw_ref = _object_ref("wikidata_raw", files["acquired.jsonl"], "application/x-ndjson")
    expected_requests = [
        {
            "kind": "http_post",
            "uri": UPSTREAM_URI,
            "parameters_sha256": record["request_sha256"],
        }
        for record in records
    ]
    expected_requests.sort(key=contracts.canonical_json_bytes)
    if receipt["source"] != "wikidata-wbgetentities" \
            or receipt["upstream_uri"] != UPSTREAM_URI \
            or receipt["tool"] != {
                "name": "wikilean-wikidata-entity-acquirer",
                "version": "1",
                "sha256": toolchain_sha,
            } \
            or receipt["requests"] != expected_requests \
            or receipt["outputs"] != [raw_ref] \
            or receipt["pin"] != {"type": "content_sha256", "value": raw_ref["sha256"]}:
        raise WikidataEntityBundleError("acquisition receipt does not bind exact acquisition")
    if receipt["batch"] != {
        "status": "complete",
        "request_set_root": contracts.acquisition_request_set_root(expected_requests),
        "requests_total": len(records),
        "requests_succeeded": len(records),
        "requests_failed": 0,
    }:
        raise WikidataEntityBundleError("acquisition receipt is not a complete batch")

    normalized_ref = _object_ref("entities", files[NORMALIZED_PATH], "application/json")
    if lineage["source"] != "wikidata-wbgetentities" \
            or lineage["mode"] != "transform" \
            or lineage["acquisition_receipt_ids"] != [receipt_id] \
            or lineage["parent_source_manifest_ids"] != [] \
            or lineage["normalization_schema"] != NORMALIZATION_SCHEMA \
            or lineage["configuration_sha256"] != NORMALIZATION_CONFIGURATION_SHA256 \
            or lineage["tool"] != {
                "name": "wikilean-wikidata-entity-normalizer",
                "version": "1",
                "sha256": ACQUIRER_WRAPPER_SHA256,
            } \
            or lineage["inputs"] != [{
                **raw_ref,
                "origin": {"kind": "acquisition_receipt", "id": receipt_id},
            }] \
            or lineage["outputs"] != [normalized_ref]:
        raise WikidataEntityBundleError("normalization lineage does not bind exact transform")

    # `toolchain` is deliberately referenced after validation so static
    # analyzers cannot mistake the validation call for an unused parse.
    if toolchain["wrapper"]["sha256"] != lineage["tool"]["sha256"]:
        raise WikidataEntityBundleError("toolchain and lineage wrapper pins diverge")
    return WikidataEntityBundle(
        path=bundle_path,
        acquisition_receipt_id=receipt_id,
        normalization_lineage_id=lineage_id,
        acquired_at=receipt["audit"]["acquired_at"],
        requested_qids=tuple(plan["qids"]),
        entities=entities,
    )


__all__ = [
    "WikidataEntityBundle",
    "WikidataEntityBundleError",
    "verify_wikidata_entity_bundle",
]
