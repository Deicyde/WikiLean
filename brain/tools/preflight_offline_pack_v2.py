#!/usr/bin/env python3
"""Preflight an offline-pack/v2 source plan without hashing or copying corpus bytes.

The pack compiler remains the authority for byte verification.  This command is a
bounded, network-free readiness check: it validates the canonical plan and inventory,
resolves exactly the declared roots, enumerates every logical selector, asks Git only
for pinned-tree metadata, and compares declared sizes with ``lstat``/Git object sizes.
It also emits explicit, non-structural concerns for source freshness, pin quality,
redistribution policy, missing receipts/lineage, and output-store capacity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import math
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema


HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
if str(BRAIN) not in sys.path:
    sys.path.insert(0, str(BRAIN))

import authority_contracts as contracts  # noqa: E402
import compile_offline_pack_v2 as compiler  # noqa: E402


PREFLIGHT_SCHEMA = "wikilean.offline-pack-preflight/v1"
DEFAULT_MAX_AGE_DAYS = 30
SPACE_RESERVE_BYTES = 64 * 1024 * 1024
SPACE_HEADROOM_RATIO = 0.15
MAX_CONTROL_FILE_BYTES = 16 * 1024 * 1024


class PreflightError(ValueError):
    """The plan cannot be safely handed to the pack compiler."""


def _fail(location: str, message: str) -> None:
    raise PreflightError(f"{location}: {message}")


def _parse_as_of(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise PreflightError("--as-of: expected an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        _fail("--as-of", "timestamp must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def _timestamp(value: str, location: str) -> dt.datetime | None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _concern(
    concerns: list[dict[str, Any]],
    severity: str,
    code: str,
    location: str,
    message: str,
    *,
    blocks: Sequence[str] = (),
) -> None:
    item: dict[str, Any] = {
        "code": code,
        "location": location,
        "message": message,
        "severity": severity,
    }
    if blocks:
        item["blocks"] = sorted(set(blocks))
    concerns.append(item)


def _required_roots(plan: Mapping[str, Any], inventory: Mapping[str, Any]) -> set[str]:
    result = {entry["id"] for entry in inventory["roots"]}
    result.add(plan["reducer"]["root"])
    result.add(plan["configuration"]["root"])
    result.add(plan["environment"]["root"])
    result.update(item["root"] for item in plan["schemas"])
    result.update(
        item["root"]
        for source in plan["sources"]
        for item in source["objects"]
    )
    return result


def _resolve_roots(
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
    roots: Mapping[str, str | os.PathLike[str]],
) -> dict[str, Path]:
    required = _required_roots(plan, inventory)
    supplied = set(roots)
    if supplied != required:
        _fail(
            "roots",
            "root bindings must exactly match the plan and inventory "
            f"(missing={sorted(required - supplied)}, unknown={sorted(supplied - required)})",
        )
    try:
        return {
            name: compiler._real_directory(path, f"roots.{name}")
            for name, path in roots.items()
        }
    except compiler.PackCompilationError as exc:
        raise PreflightError(str(exc)) from exc


def _git_declared_sizes(
    snapshot: Any,
    object_ids: Sequence[str],
    location: str,
) -> dict[str, int]:
    """Ask one local Git object database for sizes, never blob contents."""
    requested = sorted(set(object_ids))
    if not requested:
        return {}
    process = subprocess.run(
        [
            str(snapshot.git),
            "-C",
            str(snapshot.repository),
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        ],
        input="".join(object_id + "\n" for object_id in requested).encode("ascii"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=compiler._git_environment(),
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()[-500:]
        _fail(location, f"Git size query failed ({process.returncode}): {detail}")
    try:
        lines = process.stdout.decode("ascii", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise PreflightError(f"{location}: Git size query returned non-ASCII output") from exc
    if len(lines) != len(requested):
        _fail(location, "Git size query returned the wrong number of records")
    result: dict[str, int] = {}
    for expected, line in zip(requested, lines):
        parts = line.split(" ")
        if len(parts) != 3:
            _fail(location, f"malformed Git size record: {line!r}")
        actual, object_type, raw_size = parts
        if actual != expected or object_type != "blob":
            _fail(location, f"Git object is not the expected blob: {expected}")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise PreflightError(f"{location}: invalid Git object size {raw_size!r}") from exc
        if size < 0:
            _fail(location, "Git object size cannot be negative")
        result[expected] = size
    return result


def _filesystem_size(root: Path, relative: str, location: str) -> int:
    try:
        value = compiler._lstat_beneath(root, relative, location)
    except compiler.PackCompilationError as exc:
        raise PreflightError(str(exc)) from exc
    if value is None:
        _fail(location, f"source object is absent: {relative}")
    if not stat.S_ISREG(value.st_mode):
        _fail(location, f"source object is not a regular file: {relative}")
    return value.st_size


def _read_control_file(
    reference: Mapping[str, Any],
    resolved_roots: Mapping[str, Path],
    location: str,
) -> bytes:
    size = _filesystem_size(
        resolved_roots[reference["root"]],
        reference["path"],
        location,
    )
    if size != reference["bytes"]:
        _fail(
            location,
            "declared byte size does not match lstat "
            f"(planned={reference['bytes']}, actual={size})",
        )
    if size > MAX_CONTROL_FILE_BYTES:
        _fail(
            location,
            f"control file exceeds the {MAX_CONTROL_FILE_BYTES}-byte preflight limit",
        )
    try:
        raw = compiler._read_stable(
            resolved_roots[reference["root"]], reference["path"], location
        )
        compiler._verify_planned_bytes(raw, reference, location)
    except compiler.PackCompilationError as exc:
        raise PreflightError(str(exc)) from exc
    return raw


def _source_concerns(
    source: Mapping[str, Any],
    *,
    as_of: dt.datetime,
    max_age_days: int,
    concerns: list[dict[str, Any]],
) -> tuple[str, int]:
    name = source["source"]
    location = f"$.sources[{name!r}]"
    pin_type = source["pin"]["type"]
    if source["source_kind"] == "curated_git_tree":
        pin_strength = "verified-git-tree"
    elif pin_type == "content_sha256":
        pin_strength = "content-addressed"
    elif pin_type == "http_etag":
        pin_strength = "weak-opaque"
        _concern(
            concerns,
            "warning",
            "weak-http-etag-pin",
            f"{location}.pin",
            "an HTTP ETag alone does not identify an upstream revision",
            blocks=("authority", "publication"),
        )
    else:
        pin_strength = "native-unverified"
        _concern(
            concerns,
            "warning",
            "native-pin-not-locally-verifiable",
            f"{location}.pin",
            f"{pin_type} is asserted by the plan but cannot be proven offline from file metadata",
            blocks=("authority", "publication"),
        )

    audit = source.get("audit")
    if source["source_kind"] != "curated_git_tree":
        if not audit:
            _concern(
                concerns,
                "warning",
                "missing-source-audit",
                location,
                "non-Git source has no acquisition time or upstream URI",
                blocks=("authority", "publication"),
            )
        else:
            if "upstream_uri" not in audit:
                _concern(
                    concerns,
                    "warning",
                    "missing-upstream-uri",
                    f"{location}.audit",
                    "source audit does not name its upstream URI",
                    blocks=("authority", "publication"),
                )
            acquired_at = audit.get("acquired_at")
            if acquired_at is None:
                _concern(
                    concerns,
                    "warning",
                    "missing-acquired-at",
                    f"{location}.audit",
                    "source freshness cannot be assessed without acquired_at",
                    blocks=("authority", "publication"),
                )
            else:
                acquired = _timestamp(acquired_at, f"{location}.audit.acquired_at")
                if acquired is None:
                    _concern(
                        concerns,
                        "warning",
                        "invalid-acquired-at",
                        f"{location}.audit.acquired_at",
                        "timestamp is not RFC3339 with a UTC offset",
                        blocks=("authority", "publication"),
                    )
                else:
                    age = as_of - acquired
                    if age < -dt.timedelta(minutes=5):
                        _concern(
                            concerns,
                            "blocker",
                            "future-acquisition-time",
                            f"{location}.audit.acquired_at",
                            "acquisition time is later than the preflight reference time",
                            blocks=("authority", "publication"),
                        )
                    elif age > dt.timedelta(days=max_age_days):
                        _concern(
                            concerns,
                            "warning",
                            "stale-source",
                            f"{location}.audit.acquired_at",
                            f"source is older than the configured {max_age_days}-day threshold",
                            blocks=("authority", "publication"),
                        )

    receipt_count = sum(
        "receipt" in item["roles"] for item in source["objects"]
    )
    raw_names = {
        item["name"] for item in source["objects"] if "raw" in item["roles"]
    }
    normalized_names = {
        item["name"]
        for item in source["objects"]
        if "normalized" in item["roles"]
    }
    transformed = raw_names != normalized_names
    if source["source_kind"] != "curated_git_tree" and receipt_count == 0:
        _concern(
            concerns,
            "blocker",
            "missing-acquisition-receipt",
            location,
            "non-Git source has no object carrying the receipt role",
            blocks=("authority", "publication"),
        )
    if receipt_count:
        _concern(
            concerns,
            "blocker",
            "receipt-role-presence-only",
            f"{location}.objects",
            "receipt role is present, but no receipt schema or lineage content is validated yet",
            blocks=("authority", "publication"),
        )
    if transformed and receipt_count == 0:
        _concern(
            concerns,
            "blocker",
            "missing-normalization-lineage",
            f"{location}.normalization",
            "transformed outputs have no retained receipt/lineage object",
            blocks=("authority", "publication"),
        )
    elif transformed:
        _concern(
            concerns,
            "blocker",
            "normalization-lineage-unvalidated",
            f"{location}.normalization",
            "receipt presence does not yet prove cross-source normalization lineage",
            blocks=("authority", "publication"),
        )

    license_record = source["license"]
    source_redistribution = license_record["redistribution"]
    if source_redistribution == "unknown":
        _concern(
            concerns,
            "blocker",
            "unknown-source-license",
            f"{location}.license",
            "source redistribution policy is unknown",
            blocks=("authority", "publication"),
        )
    elif source_redistribution in {"restricted", "link-only"}:
        _concern(
            concerns,
            "warning",
            "non-public-source",
            f"{location}.license",
            f"source is marked {source_redistribution}",
            blocks=("authority", "publication"),
        )
    expression = license_record["expression"].casefold()
    if any(token in expression for token in ("verify", "unknown", "unlicensed", "no license")):
        _concern(
            concerns,
            "warning",
            "license-expression-needs-review",
            f"{location}.license.expression",
            "license expression contains unresolved language",
            blocks=("authority", "publication"),
        )
    object_policies = {item["redistribution"] for item in source["objects"]}
    if "unknown" in object_policies:
        _concern(
            concerns,
            "blocker",
            "unknown-object-license",
            f"{location}.objects",
            "one or more source objects have unknown redistribution policy",
            blocks=("authority", "publication"),
        )
    if source_redistribution != "allowed" and "allowed" in object_policies:
        _concern(
            concerns,
            "blocker",
            "redistribution-policy-escalation",
            f"{location}.objects",
            "an object is more permissive than its source-level policy",
            blocks=("authority", "publication"),
        )
    if object_policies & {"restricted", "link-only"}:
        _concern(
            concerns,
            "warning",
            "non-public-object",
            f"{location}.objects",
            "one or more objects must not be treated as freely redistributable",
            blocks=("authority", "publication"),
        )
    return pin_strength, receipt_count


def preflight_offline_pack_v2(
    source_plan_path: str | os.PathLike[str],
    inventory_path: str | os.PathLike[str],
    output_store: str | os.PathLike[str],
    *,
    roots: Mapping[str, str | os.PathLike[str]],
    git_executable: str | os.PathLike[str] = "git",
    as_of: dt.datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Return a canonicalizable readiness report without reading corpus contents."""
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 0:
        _fail("max_age_days", "must be a non-negative integer")
    reference_time = as_of or dt.datetime.now(dt.timezone.utc)
    if reference_time.tzinfo is None:
        _fail("as_of", "must include a UTC offset")
    reference_time = reference_time.astimezone(dt.timezone.utc)

    try:
        plan_path = compiler._real_file(source_plan_path, "source plan")
        inventory_file = compiler._real_file(inventory_path, "inventory")
        plan, plan_raw = compiler.load_source_plan(plan_path)
        inventory, inventory_raw = contracts.load_canonical_json(inventory_file)
        contracts.validate_reducer_input_inventory(inventory)
    except (OSError, compiler.PackCompilationError, contracts.VerificationError) as exc:
        raise PreflightError(str(exc)) from exc
    if inventory["inventory_id"] != plan["inventory_id"]:
        _fail("$.inventory_id", "does not match the verified reducer inventory")

    resolved_roots = _resolve_roots(plan, inventory, roots)
    try:
        store_path, store_exists = compiler._resolve_output_store(output_store)
    except compiler.PackCompilationError as exc:
        raise PreflightError(str(exc)) from exc
    for name, root in resolved_roots.items():
        if compiler._paths_overlap(store_path, root):
            _fail("output store", f"overlaps source root {name!r}")
    space_root = store_path if store_exists else store_path.parent

    try:
        git = compiler._resolve_git_executable(git_executable)
    except compiler.PackCompilationError as exc:
        raise PreflightError(str(exc)) from exc
    snapshots: dict[tuple[Path, str], Any] = {}
    git_sizes: dict[int, dict[str, int]] = {}

    def snapshot_for(root: Path, commit: str, location: str) -> Any:
        key = (root, commit)
        snapshot = snapshots.get(key)
        if snapshot is None:
            try:
                snapshot = compiler._load_git_snapshot(git, root, commit, location)
            except compiler.PackCompilationError as exc:
                raise PreflightError(str(exc)) from exc
            snapshots[key] = snapshot
        return snapshot

    def sizes_for(snapshot: Any, object_ids: Sequence[str], location: str) -> dict[str, int]:
        cache = git_sizes.setdefault(id(snapshot), {})
        missing = sorted(set(object_ids) - set(cache))
        if missing:
            cache.update(_git_declared_sizes(snapshot, missing, location))
        return cache

    reducer = plan["reducer"]
    reducer_snapshot = snapshot_for(
        resolved_roots[reducer["root"]],
        reducer["git_commit"],
        "$.reducer.git_commit",
    )
    if reducer["entrypoint"] not in inventory["scope"]:
        _fail("$.reducer.entrypoint", "must name one file in inventory.scope")
    reducer_entries = [
        reducer_snapshot.regular_blob(path, f"$.reducer.files[{index}]")
        for index, path in enumerate(inventory["scope"])
    ]
    reducer_size_map = sizes_for(
        reducer_snapshot,
        [entry.oid for entry in reducer_entries],
        "$.reducer.files",
    )
    reducer_bytes = sum(reducer_size_map[entry.oid] for entry in reducer_entries)

    source_by_name = {source["source"]: source for source in plan["sources"]}
    source_objects = {
        (source["source"], item["name"]): item
        for source in plan["sources"]
        for item in source["objects"]
    }
    curated_snapshots: dict[str, Any] = {}
    source_reports: list[dict[str, Any]] = []
    concerns: list[dict[str, Any]] = []
    redistribution = {
        key: {"bytes": 0, "objects": 0, "sources": 0}
        for key in ("allowed", "link-only", "restricted", "unknown")
    }
    digest_records: dict[str, tuple[int, str]] = {}

    for source_index, source in enumerate(plan["sources"]):
        source_name = source["source"]
        location = f"$.sources[{source_index}]"
        snapshot = None
        if source["source_kind"] == "curated_git_tree":
            object_roots = {item["root"] for item in source["objects"]}
            if len(object_roots) != 1:
                _fail(location, "curated_git_tree objects must share exactly one Git root")
            root_name = next(iter(object_roots))
            snapshot = snapshot_for(
                resolved_roots[root_name], source["pin"]["value"], f"{location}.pin"
            )
            if snapshot.tree != source["pin"]["tree"]:
                _fail(
                    f"{location}.pin.tree",
                    f"expected {source['pin']['tree']}, found {snapshot.tree}",
                )
            curated_snapshots[source_name] = snapshot
            entries = [
                snapshot.regular_blob(item["path"], f"{location}.objects[{index}]")
                for index, item in enumerate(source["objects"])
            ]
            declared_sizes = sizes_for(
                snapshot,
                [entry.oid for entry in entries],
                f"{location}.objects",
            )
        else:
            entries = []
            declared_sizes = {}

        source_bytes = 0
        source_policy = source["license"]["redistribution"]
        redistribution[source_policy]["sources"] += 1
        for object_index, item in enumerate(source["objects"]):
            object_location = f"{location}.objects[{object_index}]"
            if snapshot is not None:
                actual_size = declared_sizes[entries[object_index].oid]
            else:
                actual_size = _filesystem_size(
                    resolved_roots[item["root"]], item["path"], object_location
                )
            if actual_size != item["bytes"]:
                _fail(
                    object_location,
                    "declared byte size does not match source metadata "
                    f"(planned={item['bytes']}, actual={actual_size})",
                )
            previous = digest_records.get(item["sha256"])
            fingerprint = (item["bytes"], item["media_type"])
            if previous is not None and previous != fingerprint:
                _fail(
                    object_location,
                    "the same SHA-256 is declared with incompatible size or media type",
                )
            digest_records[item["sha256"]] = fingerprint
            source_bytes += item["bytes"]
            policy = item["redistribution"]
            redistribution[policy]["objects"] += 1
            redistribution[policy]["bytes"] += item["bytes"]

        pin_strength, receipt_count = _source_concerns(
            source,
            as_of=reference_time,
            max_age_days=max_age_days,
            concerns=concerns,
        )
        source_reports.append(
            {
                "bytes": source_bytes,
                "objects": len(source["objects"]),
                "pin_strength": pin_strength,
                "pin_type": source["pin"]["type"],
                "receipt_objects": receipt_count,
                "receipt_validation": (
                    "presence-only-unvalidated" if receipt_count else "absent"
                ),
                "redistribution": source_policy,
                "source": source_name,
                "source_kind": source["source_kind"],
            }
        )

    declarations = {item["id"]: item for item in inventory["inputs"]}
    planned_bindings = {item["input_id"]: item for item in plan["input_bindings"]}
    if set(declarations) != set(planned_bindings):
        _fail(
            "$.input_bindings",
            "must name every inventory input exactly once "
            f"(missing={sorted(set(declarations) - set(planned_bindings))}, "
            f"unknown={sorted(set(planned_bindings) - set(declarations))})",
        )

    input_reports: list[dict[str, Any]] = []
    logical_paths_by_root: dict[str, list[str]] = {}
    for input_id in sorted(declarations):
        declaration = declarations[input_id]
        binding = planned_bindings[input_id]
        location = f"$.input_bindings[{input_id!r}]"
        member_sources = sorted({member["source"] for member in binding["members"]})
        if binding["state"] == "present" and member_sources != binding["sources"]:
            _fail(location, "binding sources must exactly equal its member source set")
        binding_sources = [source_by_name[name] for name in binding["sources"]]
        if declaration["class"] == "curated_git_input":
            if len(binding_sources) != 1:
                _fail(location, "curated Git inputs require exactly one binding source")
            source = binding_sources[0]
            if source["source_kind"] != "curated_git_tree":
                _fail(location, "curated Git inputs require a curated_git_tree source")
            if {item["root"] for item in source["objects"]} != {declaration["root"]}:
                _fail(location, "curated binding source must use the inventory root")
            snapshot = curated_snapshots[source["source"]]
            actual_paths = snapshot.enumerate_input(declaration, location)
        else:
            try:
                actual_paths = compiler._enumerate_input(
                    resolved_roots[declaration["root"]], declaration, location
                )
            except compiler.PackCompilationError as exc:
                raise PreflightError(str(exc)) from exc

        planned_paths = tuple(member["path"] for member in binding["members"])
        if actual_paths != planned_paths:
            _fail(
                location,
                "declared member set does not equal the source root "
                f"(planned={list(planned_paths)}, actual={list(actual_paths)})",
            )
        expected_state = "present" if actual_paths else "absent"
        if binding["state"] != expected_state:
            _fail(location, f"expected state {expected_state!r} for the member set")
        if declaration["requirement"] == "required" and expected_state != "present":
            _fail(location, "required input is absent")
        if declaration["cardinality"] == "one" and len(actual_paths) > 1:
            _fail(location, "cardinality one input has multiple members")

        logical_paths_by_root.setdefault(declaration["root"], []).extend(actual_paths)
        planned_input_bytes = 0
        for member_index, member in enumerate(binding["members"]):
            member_location = f"{location}.members[{member_index}]"
            source_object = source_objects[(member["source"], member["object"])]
            planned_input_bytes += source_object["bytes"]
            if declaration["class"] == "curated_git_input":
                snapshot = curated_snapshots[binding["sources"][0]]
                logical_entry = snapshot.regular_blob(member["path"], member_location)
                object_entry = snapshot.regular_blob(source_object["path"], member_location)
                if logical_entry.oid != object_entry.oid:
                    _fail(member_location, "logical Git input differs from its source object")
                actual_size = sizes_for(snapshot, [logical_entry.oid], member_location)[
                    logical_entry.oid
                ]
            else:
                actual_size = _filesystem_size(
                    resolved_roots[declaration["root"]], member["path"], member_location
                )
            if actual_size != source_object["bytes"]:
                _fail(
                    member_location,
                    "logical input size differs from its source object declaration "
                    f"(planned={source_object['bytes']}, actual={actual_size})",
                )
        input_reports.append(
            {
                "bytes": planned_input_bytes,
                "input_id": input_id,
                "members": len(actual_paths),
                "requirement": declaration["requirement"],
                "state": expected_state,
            }
        )

    for root_name, paths in logical_paths_by_root.items():
        try:
            compiler._reject_path_collisions(
                paths, f"logical inputs for root {root_name!r}"
            )
        except compiler.PackCompilationError as exc:
            raise PreflightError(str(exc)) from exc

    configuration_raw = _read_control_file(
        plan["configuration"], resolved_roots, "$.configuration"
    )
    try:
        configuration_document = contracts.parse_json_bytes(
            configuration_raw, location=plan["configuration"]["path"]
        )
        configuration = compiler.build_context.ReducerConfiguration.from_document(
            configuration_document
        )
    except (contracts.VerificationError, compiler.build_context.BuildContextError) as exc:
        raise PreflightError(f"$.configuration: {exc}") from exc
    if configuration_raw != compiler.build_context.canonical_json_bytes(
        configuration.to_document()
    ):
        _fail("$.configuration", "must be exact canonical reducer configuration bytes")

    environment_raw = _read_control_file(
        plan["environment"], resolved_roots, "$.environment"
    )
    try:
        environment_document = contracts.parse_json_bytes(
            environment_raw, location=plan["environment"]["path"]
        )
        contracts.validate_execution_environment(environment_document)
    except contracts.VerificationError as exc:
        raise PreflightError(f"$.environment: {exc}") from exc
    if environment_raw != contracts.canonical_json_bytes(environment_document):
        _fail("$.environment", "must be canonical-json-v1 bytes")
    if environment_document["runner"]["git_commit"] != reducer["git_commit"]:
        _fail(
            "$.environment.runner.git_commit",
            "must equal the reducer Git commit",
        )

    schema_raw: list[bytes] = []
    for index, item in enumerate(plan["schemas"]):
        location = f"$.schemas[{index}]"
        raw = _read_control_file(item, resolved_roots, location)
        try:
            document = contracts.parse_json_bytes(raw, location=item["path"])
        except contracts.VerificationError as exc:
            raise PreflightError(f"{location}: {exc}") from exc
        if not isinstance(document, dict):
            _fail(location, "schema document must be a JSON object")
        if raw != contracts.canonical_json_bytes(document):
            _fail(location, "schema document must be canonical-json-v1 bytes")
        try:
            jsonschema.Draft202012Validator.check_schema(document)
        except jsonschema.SchemaError as exc:
            _fail(location, f"invalid Draft 2020-12 JSON Schema: {exc.message}")
        schema_raw.append(raw)

    configuration_bytes = len(configuration_raw)
    environment_bytes = len(environment_raw)
    schema_bytes = sum(map(len, schema_raw))

    digest_counts: dict[str, int] = {}
    for source in plan["sources"]:
        for item in source["objects"]:
            digest_counts[item["sha256"]] = digest_counts.get(item["sha256"], 0) + 1
    unique_object_bytes = sum(size for size, _media_type in digest_records.values())
    member_count = sum(item["members"] for item in input_reports)
    metadata_estimate = (
        len(plan_raw)
        + len(inventory_raw)
        + 64 * 1024
        + len(plan["sources"]) * 8 * 1024
        + len(digest_records) * 512
        + member_count * 512
    )
    estimated_pack_bytes = (
        unique_object_bytes
        + reducer_bytes
        + configuration_bytes
        + environment_bytes
        + schema_bytes
        + len(inventory_raw)
        + metadata_estimate
    )
    largest_duplicate_temp_bytes = max(
        (
            digest_records[digest][0]
            for digest, count in digest_counts.items()
            if count > 1
        ),
        default=0,
    )
    estimated_peak_bytes = estimated_pack_bytes + largest_duplicate_temp_bytes
    safety_margin = max(
        SPACE_RESERVE_BYTES,
        math.ceil(estimated_peak_bytes * SPACE_HEADROOM_RATIO),
    )
    recommended_free_bytes = estimated_peak_bytes + safety_margin
    try:
        free_bytes = shutil.disk_usage(space_root).free
    except OSError as exc:
        raise PreflightError(f"output store: cannot inspect free space: {exc}") from exc
    if free_bytes < estimated_peak_bytes:
        _concern(
            concerns,
            "blocker",
            "insufficient-output-space",
            "output_store",
            "free space is below the estimated compiler peak",
            blocks=("compilation",),
        )
    elif free_bytes < recommended_free_bytes:
        _concern(
            concerns,
            "warning",
            "low-output-space-headroom",
            "output_store",
            "free space covers the estimate but not the preflight safety margin",
        )

    concerns.sort(
        key=lambda item: (
            0 if item["severity"] == "blocker" else 1,
            item["code"],
            item["location"],
            item["message"],
        )
    )
    blockers = sum(item["severity"] == "blocker" for item in concerns)
    warnings = len(concerns) - blockers
    blocked_scopes = {
        scope for item in concerns for scope in item.get("blocks", [])
    }
    compile_ready = "compilation" not in blocked_scopes
    source_authority_ready = compile_ready and "authority" not in blocked_scopes
    source_publishable = (
        source_authority_ready and "publication" not in blocked_scopes
    )
    required_inputs = [
        item for item in input_reports if item["requirement"] == "required"
    ]
    return {
        "as_of": _utc_text(reference_time),
        "concerns": concerns,
        "compile_ready": compile_ready,
        "control_files": {
            "bytes": configuration_bytes + environment_bytes + schema_bytes,
            "count": 2 + len(schema_raw),
            "maximum_file_bytes": MAX_CONTROL_FILE_BYTES,
            "validation": "stable-read, SHA-256, canonical JSON, and typed config/environment checks",
        },
        "content_verification": (
            "control files are hashed and validated; corpus payloads are size-only and "
            "defer SHA-256 verification to compilation"
        ),
        "freshness_policy": {
            "configured_age_threshold_days": max_age_days,
            "source_registry_cadence_checked": False,
        },
        "inventory_id": inventory["inventory_id"],
        "inputs": input_reports,
        "ok": True,
        "readiness_scope": "source-plan-only",
        "receipt_validation": (
            "role presence only; receipt schema and normalization lineage are not yet validated"
        ),
        "runtime_environment_checked": False,
        "schema": PREFLIGHT_SCHEMA,
        "source_authority_ready": source_authority_ready,
        "source_plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "source_publishable": source_publishable,
        "sources": source_reports,
        "space": {
            "estimated_pack_bytes": estimated_pack_bytes,
            "estimated_peak_bytes": estimated_peak_bytes,
            "free_bytes": free_bytes,
            "largest_duplicate_temp_bytes": largest_duplicate_temp_bytes,
            "metadata_estimate_bytes": metadata_estimate,
            "output_store": str(store_path),
            "recommended_free_bytes": recommended_free_bytes,
            "safety_margin_bytes": safety_margin,
        },
        "summary": {
            "blockers": blockers,
            "inputs_absent": sum(item["state"] == "absent" for item in input_reports),
            "inputs_present": sum(item["state"] == "present" for item in input_reports),
            "inputs_total": len(input_reports),
            "members": member_count,
            "planned_input_bytes": sum(item["bytes"] for item in input_reports),
            "planned_object_bytes": sum(
                item["bytes"] for source in plan["sources"] for item in source["objects"]
            ),
            "required_absent": sum(item["state"] == "absent" for item in required_inputs),
            "required_present": sum(item["state"] == "present" for item in required_inputs),
            "required_total": len(required_inputs),
            "source_objects": sum(len(source["objects"]) for source in plan["sources"]),
            "sources": len(plan["sources"]),
            "unique_object_bytes": unique_object_bytes,
            "warnings": warnings,
        },
        "redistribution": redistribution,
    }


class _CanonicalArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PreflightError(f"arguments: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _CanonicalArgumentParser(description=__doc__)
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
    parser.add_argument(
        "--as-of",
        help="RFC3339 freshness reference (default: current UTC time)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"warn when a non-Git source is older than this (default: {DEFAULT_MAX_AGE_DAYS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = preflight_offline_pack_v2(
            args.plan,
            args.inventory,
            args.output_store,
            roots=compiler._parse_roots(args.root),
            git_executable=args.git,
            as_of=_parse_as_of(args.as_of),
            max_age_days=args.max_age_days,
        )
    except (
        OSError,
        PreflightError,
        compiler.PackCompilationError,
        contracts.VerificationError,
    ) as exc:
        error = {
            "error": {"message": str(exc), "type": type(exc).__name__},
            "ok": False,
            "schema": PREFLIGHT_SCHEMA,
        }
        sys.stderr.buffer.write(contracts.canonical_json_bytes(error) + b"\n")
        return 1
    sys.stdout.buffer.write(contracts.canonical_json_bytes(result) + b"\n")
    return 0 if result["source_publishable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
