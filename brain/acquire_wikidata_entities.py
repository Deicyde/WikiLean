#!/usr/bin/env python3
"""Acquire a complete, sealed ``wbgetentities`` entity bundle.

The input is an explicit canonical JSON request plan containing a non-empty,
sorted, unique list of canonical QIDs.  Requests are issued in deterministic
batches of 50.  The exact form body for every HTTP request (including any
deterministic ``no-such-entity`` bisection request) is preserved in the bundle.

Publication is private and content addressed by the clock-free normalization
lineage identity.  Audit timestamps are required evidence, but do not affect
the bundle identity or normalized entity-map bytes.  A crash before the one
atomic no-replace directory rename can leave only a private staging orphan.

The supported launcher runs CPython 3.12 with ``-I -S``.  Curl is invoked by
absolute path with its user configuration disabled and a fixed, credential-
free environment.  HTTP status and JSON content type are recorded and checked.
Automatic retry is deliberately disabled so every network attempt is visible
in receipt accounting; HTTP failures, Retry-After responses, maxlag, and every
API error except deterministic no-such-entity isolation fail closed.  No
response or stderr bytes are reflected into operator errors.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import unicodedata
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
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
    )
    for name, module, expected in reviewed:
        origin = getattr(module, "__file__", None)
        try:
            actual = Path(origin).resolve(strict=True) if origin is not None else None
            reviewed_path = expected.resolve(strict=True)
        except OSError as exc:
            return f"cannot resolve reviewed local module {name}: {exc}"
        if actual != reviewed_path:
            return f"local module {name} loaded from {actual}, expected {reviewed_path}"
    return None


_IMPORT_ORIGIN_MISMATCH = _module_origin_mismatch()
if _IMPORT_ORIGIN_MISMATCH is not None:
    raise ImportError(_IMPORT_ORIGIN_MISMATCH)


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
DEFAULT_STORE = ROOT / "catalog" / ".cache" / "wikidata" / "entity-bundles"

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
REQUEST_MEDIA_TYPE = "application/x-www-form-urlencoded"
NORMALIZED_PATH = "normalized/entities.json"

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
LOCAL_DEPENDENCIES = (
    (
        "brain/stage_io.py",
        BRAIN / "stage_io.py",
        "9b659899ce6c62709ac75b8bec2b9d83cd8550281e5d0ca2122ea6a8a805e4cf",
    ),
    (
        "brain/tools/authority_contracts.py",
        TOOLS / "authority_contracts.py",
        "fb2f105b2cad2a5ceed38925694f8da1766b57774a7e730078f537b68da018c6",
    ),
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


class WikidataEntityAcquisitionError(RuntimeError):
    """The request plan, remote transcript, or publication is untrusted."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class RemoteResponse:
    body: bytes
    http_status: int
    content_type: str


ResponseProvider = Callable[[tuple[str, ...], int], bytes | RemoteResponse]


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
        raise WikidataEntityAcquisitionError(
            "Wikidata entity acquisition requires the supported isolated "
            "CPython 3.12 launcher (-I -S)"
        )
    return flags


def _verify_local_module_origins() -> None:
    mismatch = _module_origin_mismatch()
    if mismatch is not None:
        raise WikidataEntityAcquisitionError(mismatch)


def _verify_loaded_program() -> None:
    if _file_sha256(Path(__file__)) != LOADED_SCRIPT_SHA256:
        raise WikidataEntityAcquisitionError(
            "Wikidata entity acquisition program changed during execution"
        )


def _resolved_python() -> Path:
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12):
        raise WikidataEntityAcquisitionError(
            f"Wikidata entity acquisition requires CPython 3.12, found "
            f"{platform.python_implementation()} {platform.python_version()}"
        )
    try:
        resolved = Path(sys.executable).resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise WikidataEntityAcquisitionError("cannot resolve the Python executable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
            or not os.access(resolved, os.X_OK):
        raise WikidataEntityAcquisitionError("Python executable is not a regular executable")
    return resolved


def _resolved_curl() -> Path:
    for candidate in (Path("/usr/bin/curl"), Path("/bin/curl")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(metadata.st_mode) and stat.S_ISREG(metadata.st_mode) \
                and os.access(candidate, os.X_OK):
            return candidate
    raise WikidataEntityAcquisitionError(
        "reviewable system curl is absent from /usr/bin/curl and /bin/curl"
    )


def _local_dependency_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for relative, path, expected in LOCAL_DEPENDENCIES:
        actual = _file_sha256(path)
        if actual != expected:
            raise WikidataEntityAcquisitionError(
                f"Wikidata acquisition dependency changed: {relative}; "
                f"expected {expected}, got {actual}"
            )
        records.append({"path": relative, "sha256": actual})
    return records


def _sanitized_environment() -> dict[str, str]:
    return dict(FORCED_ENVIRONMENT)


def _pinned_toolchain(
    *, probe_runner: Runner = subprocess.run
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    startup_flags = _verify_isolated_startup()
    _verify_local_module_origins()
    python = _resolved_python()
    curl = _resolved_curl()
    environment = _sanitized_environment()
    try:
        probe = probe_runner(
            [str(curl), "--disable", "--version"],
            capture_output=True,
            timeout=10,
            env=environment,
            cwd=ROOT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WikidataEntityAcquisitionError("cannot inspect the curl runtime") from exc
    if probe.returncode != 0 or not isinstance(probe.stdout, bytes):
        raise WikidataEntityAcquisitionError("curl runtime inspection failed")
    try:
        first_line = probe.stdout.splitlines()[0].decode("ascii")
    except (IndexError, UnicodeDecodeError) as exc:
        raise WikidataEntityAcquisitionError("curl runtime reported an invalid version") from exc
    if not first_line.startswith("curl ") or len(first_line) > 512:
        raise WikidataEntityAcquisitionError("curl runtime reported an invalid version")
    toolchain = {
        "schema": TOOLCHAIN_SCHEMA,
        "invocation": {
            "uri": UPSTREAM_URI,
            "arguments": CURL_ARGUMENTS,
            "forwarded_environment": FORWARDED_ENVIRONMENT,
            "forced_environment": FORCED_ENVIRONMENT,
            "response_policy": HTTP_RESPONSE_POLICY,
        },
        "curl": {
            "version": first_line,
            "sha256": _file_sha256(curl),
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "sha256": _file_sha256(python),
            "startup_flags": startup_flags,
        },
        "local_dependencies": _local_dependency_records(),
        "wrapper": {"sha256": LOADED_SCRIPT_SHA256},
    }
    toolchain_bytes = contracts.canonical_json_bytes(toolchain)
    tool = {
        "name": "wikilean-wikidata-entity-acquirer",
        "version": "1",
        "sha256": _sha256(toolchain_bytes),
    }
    _verify_runtime_closure(toolchain)
    return curl, tool, toolchain


def _verify_runtime_closure(toolchain: Mapping[str, Any]) -> None:
    _verify_loaded_program()
    _verify_local_module_origins()
    python = _resolved_python()
    curl = _resolved_curl()
    recorded_python = toolchain.get("python")
    if not isinstance(recorded_python, Mapping) or recorded_python != {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "sha256": _file_sha256(python),
        "startup_flags": REQUIRED_PYTHON_STARTUP_FLAGS,
    }:
        raise WikidataEntityAcquisitionError("Python runtime closure changed during acquisition")
    recorded_curl = toolchain.get("curl")
    if not isinstance(recorded_curl, Mapping) \
            or recorded_curl.get("sha256") != _file_sha256(curl) \
            or not isinstance(recorded_curl.get("version"), str):
        raise WikidataEntityAcquisitionError("curl runtime closure changed during acquisition")
    if toolchain.get("local_dependencies") != _local_dependency_records():
        raise WikidataEntityAcquisitionError("local dependency closure changed during acquisition")
    if toolchain.get("wrapper") != {"sha256": LOADED_SCRIPT_SHA256}:
        raise WikidataEntityAcquisitionError("acquisition wrapper changed during execution")
    if toolchain.get("schema") != TOOLCHAIN_SCHEMA or toolchain.get("invocation") != {
        "uri": UPSTREAM_URI,
        "arguments": CURL_ARGUMENTS,
        "forwarded_environment": FORWARDED_ENVIRONMENT,
        "forced_environment": FORCED_ENVIRONMENT,
        "response_policy": HTTP_RESPONSE_POLICY,
    }:
        raise WikidataEntityAcquisitionError("toolchain invocation is not the reviewed policy")


def _strict_json_bytes(data: bytes, *, location: str) -> Any:
    if len(data) > MAX_RESPONSE_BYTES:
        raise WikidataEntityAcquisitionError(f"{location}: response exceeds size limit")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise WikidataEntityAcquisitionError(
                    f"{location}: duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise WikidataEntityAcquisitionError(
            f"{location}: non-finite JSON number {value!r}"
        )

    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except WikidataEntityAcquisitionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikidataEntityAcquisitionError(f"{location}: invalid JSON") from exc


def _canonical_line(value: Any) -> bytes:
    return contracts.canonical_artifact_json_bytes(value) + b"\n"


def _request_body(qids: Sequence[str]) -> bytes:
    fields = [*REQUEST_FIELDS, ("ids", "|".join(qids))]
    return urllib.parse.urlencode(
        fields,
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
    direct = len(entities) - missing - redirected
    return {
        "requested": len(entities),
        "direct": direct,
        "redirected": redirected,
        "missing": missing,
    }


def validate_request_plan_bytes(data: bytes) -> dict[str, Any]:
    try:
        value = contracts.parse_json_bytes(data, location="request plan")
    except contracts.VerificationError as exc:
        raise WikidataEntityAcquisitionError(str(exc)) from exc
    if not isinstance(value, dict) or set(value) != {"schema", "qids"}:
        raise WikidataEntityAcquisitionError(
            "request plan: expected exactly schema and qids"
        )
    if value["schema"] != REQUEST_PLAN_SCHEMA:
        raise WikidataEntityAcquisitionError("request plan: unexpected schema")
    qids = value["qids"]
    if not isinstance(qids, list) or not qids:
        raise WikidataEntityAcquisitionError("request plan.qids: expected a non-empty array")
    if len(qids) > MAX_PLAN_QIDS:
        raise WikidataEntityAcquisitionError(
            f"request plan.qids: exceeds maximum of {MAX_PLAN_QIDS}"
        )
    for index, qid in enumerate(qids):
        if not isinstance(qid, str) or QID_RE.fullmatch(qid) is None:
            raise WikidataEntityAcquisitionError(
                f"request plan.qids[{index}]: expected a canonical QID"
            )
    if qids != sorted(set(qids)):
        raise WikidataEntityAcquisitionError(
            "request plan.qids: entries must be unique and lexicographically sorted"
        )
    if data != contracts.canonical_json_bytes(value):
        raise WikidataEntityAcquisitionError("request plan: expected canonical JSON bytes")
    return value


def _stable_file_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_stable_regular(path: Path) -> tuple[bytes, tuple[int, ...]]:
    path = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WikidataEntityAcquisitionError(f"cannot open request plan: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(opened.st_mode) \
                or not os.path.samestat(before, opened):
            raise WikidataEntityAcquisitionError("request plan is not a stable regular file")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _stable_file_state(opened) != _stable_file_state(after) \
                or not os.path.samestat(after, current):
            raise WikidataEntityAcquisitionError("request plan changed while being read")
        return b"".join(chunks), _stable_file_state(after)
    finally:
        os.close(descriptor)


def _nfc_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or "\x00" in value \
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise WikidataEntityAcquisitionError(f"{location}: expected a valid string")
    return unicodedata.normalize("NFC", value)


def _no_such_error_matches(error: Mapping[str, Any], qid: str) -> bool:
    """Require singleton missing evidence to name exactly its requested QID."""
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
    if not isinstance(payload, dict):
        raise WikidataEntityAcquisitionError(f"{location}: response is not an object")
    allowed = {"success", "entities", "redirects"}
    if set(payload) - allowed:
        raise WikidataEntityAcquisitionError(f"{location}: unexpected response fields")
    if type(payload.get("success")) is not int or payload["success"] != 1:
        raise WikidataEntityAcquisitionError(
            f"{location}: expected exact formatversion=2 success marker"
        )
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise WikidataEntityAcquisitionError(f"{location}: missing entities object")
    for key in entities:
        if not isinstance(key, str) or QID_RE.fullmatch(key) is None:
            raise WikidataEntityAcquisitionError(f"{location}: malformed entity key")

    redirect_rows = payload.get("redirects", [])
    if not isinstance(redirect_rows, list):
        raise WikidataEntityAcquisitionError(f"{location}: redirects is not an array")
    redirects: dict[str, str] = {}
    for index, row in enumerate(redirect_rows):
        if not isinstance(row, dict) or set(row) != {"from", "to"} \
                or not isinstance(row.get("from"), str) \
                or not isinstance(row.get("to"), str) \
                or QID_RE.fullmatch(row["from"]) is None \
                or QID_RE.fullmatch(row["to"]) is None:
            raise WikidataEntityAcquisitionError(
                f"{location}.redirects[{index}]: malformed redirect"
            )
        source, target = row["from"], row["to"]
        if source in redirects:
            raise WikidataEntityAcquisitionError(
                f"{location}.redirects[{index}]: duplicate redirect source"
            )
        redirects[source] = target

    output: dict[str, dict[str, Any]] = {}
    used_redirects: set[str] = set()
    used_entity_keys: set[str] = set()
    for requested in requested_qids:
        top_resolved = requested
        seen: set[str] = set()
        while top_resolved in redirects:
            if top_resolved in seen:
                raise WikidataEntityAcquisitionError(f"{location}: redirect cycle")
            seen.add(top_resolved)
            used_redirects.add(top_resolved)
            top_resolved = redirects[top_resolved]
        available = [key for key in (requested, top_resolved) if key in entities]
        if not available:
            raise WikidataEntityAcquisitionError(
                f"{location}: omitted requested QID {requested}"
            )
        if len(set(available)) != 1:
            raise WikidataEntityAcquisitionError(
                f"{location}: ambiguous entity rows for requested QID {requested}"
            )
        entity_key = available[0]
        used_entity_keys.add(entity_key)
        entity = entities[entity_key]
        if not isinstance(entity, dict):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} is not an object"
            )
        if "missing" in entity:
            if entity != {"id": requested, "missing": ""} \
                    or top_resolved != requested:
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} has invalid missing row"
                )
            output[requested] = {"missing": True}
            continue
        if "id" not in entity:
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} lacks an explicit id"
            )
        raw_entity_id = entity.get("id", top_resolved)
        if not isinstance(raw_entity_id, str) or QID_RE.fullmatch(raw_entity_id) is None:
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed id"
            )
        local_redirect = entity.get("redirects")
        if raw_entity_id != requested:
            # Live wbgetentities binds redirects on the requested-key entity.
            # Top-level redirect rows, when present, are corroborating evidence
            # only and can never substitute for this exact local binding.
            if local_redirect != {"from": requested, "to": raw_entity_id}:
                raise WikidataEntityAcquisitionError(
                    f"{location}: redirected entity {entity_key} lacks exact "
                    "entity-local binding"
                )
        elif local_redirect is not None:
            raise WikidataEntityAcquisitionError(
                f"{location}: non-redirected entity {entity_key} has redirect metadata"
            )
        if top_resolved != requested and raw_entity_id != top_resolved:
            raise WikidataEntityAcquisitionError(
                f"{location}: top-level and entity-local redirects conflict"
            )
        resolved = raw_entity_id
        containers: dict[str, dict[str, Any]] = {}
        for field in ("labels", "aliases", "descriptions", "claims", "sitelinks"):
            value = entity.get(field, {})
            if not isinstance(value, dict):
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} has malformed {field}"
                )
            containers[field] = value

        label_row = containers["labels"].get("en")
        if label_row is not None and (
            not isinstance(label_row, dict) or "value" not in label_row
        ):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed English label"
            )
        label = None if label_row is None else _nfc_string(
            label_row["value"], f"{location}.entities.{entity_key}.labels.en.value"
        )

        alias_rows = containers["aliases"].get("en", [])
        if not isinstance(alias_rows, list):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed English aliases"
            )
        aliases: list[str] = []
        for index, row in enumerate(alias_rows):
            if not isinstance(row, dict) or "value" not in row:
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} alias {index} is malformed"
                )
            aliases.append(_nfc_string(
                row["value"],
                f"{location}.entities.{entity_key}.aliases.en[{index}].value",
            ))

        description_row = containers["descriptions"].get("en")
        if description_row is not None and (
            not isinstance(description_row, dict) or "value" not in description_row
        ):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed English description"
            )
        description = None if description_row is None else _nfc_string(
            description_row["value"],
            f"{location}.entities.{entity_key}.descriptions.en.value",
        )

        p31_rows = containers["claims"].get("P31", [])
        if not isinstance(p31_rows, list):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed P31 claims"
            )
        classes: list[str] = []
        for index, claim in enumerate(p31_rows):
            if not isinstance(claim, dict) or not isinstance(claim.get("mainsnak"), dict):
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} P31 claim {index} is malformed"
                )
            datavalue = claim["mainsnak"].get("datavalue")
            if datavalue is None:
                continue
            if not isinstance(datavalue, dict) or not isinstance(datavalue.get("value"), dict):
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} P31 value {index} is malformed"
                )
            class_qid = datavalue["value"].get("id")
            if not isinstance(class_qid, str) or QID_RE.fullmatch(class_qid) is None:
                raise WikidataEntityAcquisitionError(
                    f"{location}: entity {entity_key} P31 value {index} is malformed"
                )
            classes.append(class_qid)

        enwiki = containers["sitelinks"].get("enwiki")
        if enwiki is not None and (
            not isinstance(enwiki, dict) or "title" not in enwiki
        ):
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed enwiki sitelink"
            )
        title = None if enwiki is None else _nfc_string(
            enwiki["title"], f"{location}.entities.{entity_key}.sitelinks.enwiki.title"
        )
        lastrevid = entity.get("lastrevid")
        if type(lastrevid) is not int or not 0 <= lastrevid <= contracts.MAX_SAFE_INTEGER:
            raise WikidataEntityAcquisitionError(
                f"{location}: entity {entity_key} has malformed lastrevid"
            )
        modified = _nfc_string(
            entity.get("modified"), f"{location}.entities.{entity_key}.modified"
        )
        _timestamp(modified)
        output[requested] = {
            "qid": resolved,
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
        raise WikidataEntityAcquisitionError(f"{location}: unreferenced redirect row")
    if set(entities) != used_entity_keys:
        raise WikidataEntityAcquisitionError(f"{location}: unreferenced entity row")
    return output


def _execute_plan(
    plan: Mapping[str, Any], response_provider: ResponseProvider
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    entities: dict[str, dict[str, Any]] = {}

    def execute(qids: tuple[str, ...]) -> None:
        request_index = len(records)
        body = _request_body(qids)
        provided = response_provider(qids, request_index)
        response = (
            RemoteResponse(provided, 200, "application/json")
            if isinstance(provided, bytes)
            else provided
        )
        if not isinstance(response, RemoteResponse) or not isinstance(response.body, bytes):
            raise WikidataEntityAcquisitionError(
                f"request {request_index}: response provider returned invalid response"
            )
        if response.http_status != 200:
            raise WikidataEntityAcquisitionError(
                f"request {request_index}: unexpected HTTP status {response.http_status}"
            )
        media_type = response.content_type.split(";", 1)[0].strip().casefold()
        if media_type != "application/json":
            raise WikidataEntityAcquisitionError(
                f"request {request_index}: unexpected response content type"
            )
        payload = _strict_json_bytes(response.body, location=f"request {request_index}")
        records.append({
            "request_index": request_index,
            "qids": list(qids),
            "request_sha256": _sha256(body),
            "http_status": response.http_status,
            "content_type": response.content_type,
            "response_base64": base64.b64encode(response.body).decode("ascii"),
        })
        if isinstance(payload, dict) and "error" in payload:
            if set(payload) not in ({"error"}, {"error", "servedby"}):
                raise WikidataEntityAcquisitionError(
                    f"request {request_index}: malformed API error response"
                )
            error = payload["error"]
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise WikidataEntityAcquisitionError(
                    f"request {request_index}: malformed API error response"
                )
            if "servedby" in payload and not isinstance(payload["servedby"], str):
                raise WikidataEntityAcquisitionError(
                    f"request {request_index}: malformed API error response"
                )
            if error["code"] != "no-such-entity":
                raise WikidataEntityAcquisitionError(
                    f"request {request_index}: Wikidata returned an API error"
                )
            if len(qids) == 1:
                if not _no_such_error_matches(error, qids[0]):
                    raise WikidataEntityAcquisitionError(
                        f"request {request_index}: singleton no-such-entity "
                        "does not identify the requested QID"
                    )
                entities[qids[0]] = {"missing": True}
                return
            midpoint = len(qids) // 2
            execute(qids[:midpoint])
            execute(qids[midpoint:])
            return
        batch = _normalize_success_payload(
            payload, qids, location=f"request {request_index}"
        )
        overlap = set(batch) & set(entities)
        if overlap:
            raise WikidataEntityAcquisitionError(
                f"request {request_index}: duplicate normalized QIDs"
            )
        entities.update(batch)

    qids = plan["qids"]
    for offset in range(0, len(qids), BATCH_SIZE):
        execute(tuple(qids[offset:offset + BATCH_SIZE]))
    if sorted(entities) != qids:
        raise WikidataEntityAcquisitionError("acquisition did not cover the complete request plan")
    return records, entities


def _run_curl_request(
    qids: tuple[str, ...],
    request_index: int,
    *,
    curl: Path,
    toolchain: Mapping[str, Any],
    runner: Runner,
) -> RemoteResponse:
    body = _request_body(qids)
    _verify_runtime_closure(toolchain)
    command = [str(curl), *CURL_ARGUMENTS, UPSTREAM_URI]
    try:
        result = runner(
            command,
            input=body,
            capture_output=True,
            timeout=120,
            env=_sanitized_environment(),
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl timed out"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl execution failed"
        ) from exc
    _verify_runtime_closure(toolchain)
    if result.returncode != 0:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl failed with exit status {result.returncode}"
        )
    if not isinstance(result.stdout, bytes) or not result.stdout:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl returned an empty or non-bytes response"
        )
    if len(result.stdout) > MAX_RESPONSE_BYTES:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: response exceeds size limit"
        )
    if not isinstance(result.stderr, bytes):
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl returned invalid HTTP metadata"
        )
    try:
        metadata = result.stderr.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl returned invalid HTTP metadata"
        ) from exc
    if len(metadata) != 1:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl returned invalid HTTP metadata"
        )
    fields = metadata[0].split("\t")
    if len(fields) != 3 or fields[0] != "wikilean-http-v1" \
            or not fields[1].isdigit():
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: curl returned invalid HTTP metadata"
        )
    status = int(fields[1])
    content_type = fields[2]
    if status != HTTP_RESPONSE_POLICY["status"]:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: unexpected HTTP status {status}"
        )
    if content_type.split(";", 1)[0].strip().casefold() \
            != HTTP_RESPONSE_POLICY["content_type"]:
        raise WikidataEntityAcquisitionError(
            f"request {request_index}: unexpected response content type"
        )
    return RemoteResponse(result.stdout, status, content_type)


def _object_ref(name: str, data: bytes, media_type: str) -> dict[str, Any]:
    return {
        "object": name,
        "sha256": _sha256(data),
        "bytes": len(data),
        "media_type": media_type,
    }


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or contracts.UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise WikidataEntityAcquisitionError(
            "audit timestamp must be RFC3339 UTC ending in Z"
        )
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WikidataEntityAcquisitionError("audit timestamp is not valid RFC3339 UTC") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise WikidataEntityAcquisitionError("audit timestamp must be UTC")
    return value


def _bundle_files(
    plan_bytes: bytes,
    records: Sequence[dict[str, Any]],
    entities: Mapping[str, dict[str, Any]],
    *,
    acquisition_tool: dict[str, Any],
    acquisition_toolchain: dict[str, Any],
    audit_time: str,
) -> tuple[str, dict[str, bytes]]:
    plan = validate_request_plan_bytes(plan_bytes)
    if sorted(entities) != plan["qids"]:
        raise WikidataEntityAcquisitionError("normalized map does not cover request plan")
    toolchain_bytes = contracts.canonical_json_bytes(acquisition_toolchain)
    if acquisition_tool != {
        "name": "wikilean-wikidata-entity-acquirer",
        "version": "1",
        "sha256": _sha256(toolchain_bytes),
    }:
        raise WikidataEntityAcquisitionError("acquisition tool does not bind toolchain bytes")

    raw = b"".join(_canonical_line(record) for record in records)
    normalized = contracts.canonical_artifact_json_bytes({
        "schema": NORMALIZATION_SCHEMA,
        "entities": dict(entities),
    })
    request_files: dict[str, bytes] = {}
    receipt_requests: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if record.get("request_index") != index:
            raise WikidataEntityAcquisitionError("request transcript indices are not contiguous")
        qids = record.get("qids")
        if not isinstance(qids, list):
            raise WikidataEntityAcquisitionError("request transcript has malformed QIDs")
        body = _request_body(qids)
        digest = _sha256(body)
        if record.get("request_sha256") != digest:
            raise WikidataEntityAcquisitionError("request transcript digest mismatch")
        request_files[f"requests/{index:06d}.form"] = body
        receipt_requests.append({
            "kind": "http_post",
            "uri": UPSTREAM_URI,
            "parameters_sha256": digest,
        })
    receipt_requests.sort(key=contracts.canonical_json_bytes)
    if len(receipt_requests) != len({item["parameters_sha256"] for item in receipt_requests}):
        raise WikidataEntityAcquisitionError("request transcript contains duplicate preimages")

    raw_ref = _object_ref("wikidata_raw", raw, "application/x-ndjson")
    receipt: dict[str, Any] = {
        "schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1,
        "acquisition_receipt_id": "sha256:" + "0" * 64,
        "source": "wikidata-wbgetentities",
        "upstream_uri": UPSTREAM_URI,
        "pin": {"type": "content_sha256", "value": raw_ref["sha256"]},
        "tool": acquisition_tool,
        "requests": receipt_requests,
        "batch": {
            "status": "complete",
            "request_set_root": contracts.acquisition_request_set_root(receipt_requests),
            "requests_total": len(receipt_requests),
            "requests_succeeded": len(receipt_requests),
            "requests_failed": 0,
        },
        "outputs": [raw_ref],
        "audit": {"acquired_at": _timestamp(audit_time)},
    }
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)

    normalized_ref = _object_ref("entities", normalized, "application/json")
    lineage: dict[str, Any] = {
        "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
        "normalization_lineage_id": "sha256:" + "0" * 64,
        "source": "wikidata-wbgetentities",
        "mode": "transform",
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]],
        "parent_source_manifest_ids": [],
        "normalization_schema": NORMALIZATION_SCHEMA,
        "configuration_sha256": NORMALIZATION_CONFIGURATION_SHA256,
        "tool": {
            "name": "wikilean-wikidata-entity-normalizer",
            "version": "1",
            "sha256": LOADED_SCRIPT_SHA256,
        },
        "inputs": [{
            **raw_ref,
            "origin": {
                "kind": "acquisition_receipt",
                "id": receipt["acquisition_receipt_id"],
            },
        }],
        "outputs": [normalized_ref],
        "result": "complete",
        "audit": {"normalized_at": _timestamp(audit_time)},
    }
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    contracts.validate_normalization_lineage(lineage)
    bundle_id = lineage["normalization_lineage_id"]

    deterministic = {
        "request-plan.json": plan_bytes,
        "toolchain.json": toolchain_bytes,
        "acquired.jsonl": raw,
        NORMALIZED_PATH: normalized,
        **request_files,
    }
    media_types = {
        "request-plan.json": "application/json",
        "toolchain.json": "application/json",
        "acquired.jsonl": "application/x-ndjson",
        NORMALIZED_PATH: "application/json",
        **{path: REQUEST_MEDIA_TYPE for path in request_files},
    }
    members = [
        {
            "path": path,
            "sha256": _sha256(data),
            "bytes": len(data),
            "media_type": media_types[path],
        }
        for path, data in sorted(deterministic.items())
    ]
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "identity_basis": "normalization_lineage_id",
        "request_plan_sha256": _sha256(plan_bytes),
        "requested_qid_set_root": requested_qid_set_root(plan["qids"]),
        "summary": _entity_summary(entities),
        "toolchain_sha256": _sha256(toolchain_bytes),
        "acquisition_receipt_id": receipt["acquisition_receipt_id"],
        "normalization_lineage_id": lineage["normalization_lineage_id"],
        "members": members,
        "evidence": {
            "acquisition_receipt": "acquisition-receipt.json",
            "normalization_lineage": "normalization-lineage.json",
        },
    }
    return bundle_id, {
        **deterministic,
        "acquisition-receipt.json": contracts.canonical_json_bytes(receipt),
        "normalization-lineage.json": contracts.canonical_json_bytes(lineage),
        "bundle.json": contracts.canonical_json_bytes(manifest),
    }


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WikidataEntityAcquisitionError(f"not a real directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise WikidataEntityAcquisitionError(f"directory must have mode 0700: {path}")


def _prepare_store(store: Path) -> tuple[Path, Path]:
    store = Path(store).absolute()
    anchor = store.parent
    while not anchor.exists() and not anchor.is_symlink() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        anchor_metadata = anchor.lstat()
    except OSError as exc:
        raise WikidataEntityAcquisitionError("cannot resolve bundle-store ancestor") from exc
    if stat.S_ISLNK(anchor_metadata.st_mode) or not stat.S_ISDIR(anchor_metadata.st_mode):
        raise WikidataEntityAcquisitionError(
            f"bundle-store ancestor is not a real directory: {anchor}"
        )
    if not store.exists() and not store.is_symlink():
        stage_io.ensure_private_directory(anchor, store)
    _require_private_directory(store)
    staging = store / ".staging"
    if not staging.exists() and not staging.is_symlink():
        stage_io.ensure_private_directory(store, staging)
    _require_private_directory(staging)
    return store, staging


def staging_orphans(store: Path) -> tuple[Path, ...]:
    staging = Path(store).absolute() / ".staging"
    if not staging.exists() and not staging.is_symlink():
        return ()
    _require_private_directory(staging)
    orphans: list[Path] = []
    for child in sorted(staging.iterdir(), key=lambda path: path.name):
        _require_private_directory(child)
        if not child.name.endswith(".tmp"):
            raise WikidataEntityAcquisitionError(
                f"unexpected entry in staging directory: {child}"
            )
        orphans.append(child)
    return tuple(orphans)


def _write_stage(scratch: stage_io.OwnedDirectory, files: Mapping[str, bytes]) -> None:
    directories = sorted(
        {
            Path(relative).parent.as_posix()
            for relative in files
            if Path(relative).parent.as_posix() != "."
        },
        key=lambda item: (item.count("/"), item),
    )
    for relative in directories:
        directory = scratch.path / relative
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        directory.chmod(0o700)
        stage_io.fsync_directory(directory)
        stage_io.fsync_directory(directory.parent)
    for relative, data in sorted(files.items()):
        stage_io.write_bytes_exclusive(scratch.path / relative, data, mode=0o644)
    stage_io.fsync_directory(scratch.path)


def _read_canonical_object(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        value = contracts.parse_json_bytes(data, location=str(path))
    except (OSError, contracts.VerificationError) as exc:
        raise WikidataEntityAcquisitionError(str(exc)) from exc
    if not isinstance(value, dict) or data != contracts.canonical_json_bytes(value):
        raise WikidataEntityAcquisitionError(f"{path}: expected canonical JSON object")
    return value


def _verify_published(
    target: Path, bundle_id: str, expected_files: Mapping[str, bytes]
) -> None:
    _require_private_directory(target)
    actual_paths: set[str] = set()
    for directory, names, filenames in os.walk(target, followlinks=False):
        directory_path = Path(directory)
        _require_private_directory(directory_path)
        names.sort()
        filenames.sort()
        for name in names:
            _require_private_directory(directory_path / name)
        for name in filenames:
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
                    or stat.S_IMODE(metadata.st_mode) != 0o644 \
                    or metadata.st_nlink != 1:
                raise WikidataEntityAcquisitionError(
                    f"published bundle has invalid file member: {child}"
                )
            actual_paths.add(child.relative_to(target).as_posix())
    if actual_paths != set(expected_files):
        raise WikidataEntityAcquisitionError("published bundle member set mismatch")
    for relative, expected in expected_files.items():
        if relative in {"acquisition-receipt.json", "normalization-lineage.json"}:
            continue
        if (target / relative).read_bytes() != expected:
            raise WikidataEntityAcquisitionError(
                f"published bundle member mismatch: {relative}"
            )
    receipt = _read_canonical_object(target / "acquisition-receipt.json")
    lineage = _read_canonical_object(target / "normalization-lineage.json")
    try:
        contracts.validate_acquisition_receipt(receipt)
        contracts.validate_normalization_lineage(lineage)
    except contracts.VerificationError as exc:
        raise WikidataEntityAcquisitionError(str(exc)) from exc
    expected_receipt = contracts.parse_json_bytes(
        expected_files["acquisition-receipt.json"], location="candidate receipt"
    )
    if lineage["normalization_lineage_id"] != bundle_id \
            or receipt["acquisition_receipt_id"] != expected_receipt["acquisition_receipt_id"] \
            or lineage["acquisition_receipt_ids"] != [receipt["acquisition_receipt_id"]]:
        raise WikidataEntityAcquisitionError("published evidence identity mismatch")


def _publish(
    plan_bytes: bytes,
    records: Sequence[dict[str, Any]],
    entities: Mapping[str, dict[str, Any]],
    *,
    store: Path,
    acquisition_tool: dict[str, Any],
    acquisition_toolchain: dict[str, Any],
    audit_time: str,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    _verify_runtime_closure(acquisition_toolchain)
    bundle_id, files = _bundle_files(
        plan_bytes,
        records,
        entities,
        acquisition_tool=acquisition_tool,
        acquisition_toolchain=acquisition_toolchain,
        audit_time=audit_time,
    )
    store, staging = _prepare_store(store)
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
            # Re-verify the exact candidate after the final extensibility/race
            # point and before the atomic rename.  A mutated stage is removed
            # by the owned-directory context and is never made visible.
            _verify_published(scratch.path, bundle_id, files)
            _verify_runtime_closure(acquisition_toolchain)
            try:
                stage_io.publish_directory_no_replace(scratch, target)
                published = True
            except FileExistsError:
                pass
    finally:
        if published:
            stage_io.fsync_directory(store)
    _verify_published(target, bundle_id, files)
    return target


def publish_transcript(
    plan_bytes: bytes,
    responses: Iterable[bytes],
    *,
    store: Path,
    acquisition_tool: dict[str, Any],
    acquisition_toolchain: dict[str, Any],
    audit_time: str,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    """Hermetically validate and publish a pre-acquired response transcript."""
    plan = validate_request_plan_bytes(plan_bytes)
    iterator = iter(responses)

    def provide(_qids: tuple[str, ...], request_index: int) -> bytes:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise WikidataEntityAcquisitionError(
                f"request transcript truncated before request {request_index}"
            ) from exc

    records, entities = _execute_plan(plan, provide)
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        raise WikidataEntityAcquisitionError("request transcript has unconsumed responses")
    return _publish(
        plan_bytes,
        records,
        entities,
        store=store,
        acquisition_tool=acquisition_tool,
        acquisition_toolchain=acquisition_toolchain,
        audit_time=audit_time,
        before_publish=before_publish,
    )


def acquire_bundle(
    request_plan_path: Path,
    *,
    store: Path,
    audit_time: str,
    runner: Runner = subprocess.run,
    probe_runner: Runner = subprocess.run,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    _verify_isolated_startup()
    _verify_loaded_program()
    plan_path = Path(request_plan_path).absolute()
    plan_bytes, plan_state = _read_stable_regular(plan_path)
    plan = validate_request_plan_bytes(plan_bytes)
    curl, tool, toolchain = _pinned_toolchain(probe_runner=probe_runner)

    def provide(qids: tuple[str, ...], request_index: int) -> bytes:
        return _run_curl_request(
            qids,
            request_index,
            curl=curl,
            toolchain=toolchain,
            runner=runner,
        )

    records, entities = _execute_plan(plan, provide)
    current_bytes, current_state = _read_stable_regular(plan_path)
    if current_bytes != plan_bytes or current_state != plan_state:
        raise WikidataEntityAcquisitionError("request plan changed during acquisition")
    _verify_runtime_closure(toolchain)
    return _publish(
        plan_bytes,
        records,
        entities,
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
    except WikidataEntityAcquisitionError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_plan", type=Path)
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="private content-addressed entity-bundle store",
    )
    parser.add_argument(
        "--audit-time",
        default=_now_utc(),
        help="RFC3339 UTC audit timestamp (excluded from logical identities)",
    )
    args = parser.parse_args(argv)
    try:
        target = acquire_bundle(
            args.request_plan,
            store=args.store,
            audit_time=args.audit_time,
        )
    except (WikidataEntityAcquisitionError, OSError, ValueError) as exc:
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
