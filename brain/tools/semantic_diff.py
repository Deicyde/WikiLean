#!/usr/bin/env python3
"""Compare two current WikiLean Brain snapshots by semantic identity.

Each operand may be a Brain data directory, a release manifest, or a local release
root containing release.json, release-manifest.json, or manifest.json.

Example:
    python3 brain/tools/semantic_diff.py --from old/brain/data --to new/brain/data
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import authority_contracts as contracts

SCHEMA = "wikilean.semantic-diff/v1"
DIRECT_REQUIRED = {
    "nodes": "nodes.jsonl",
    "edges": "edges.jsonl",
    "cells": "cells.jsonl",
    "frontier": "frontier.jsonl",
}
DIRECT_OPTIONAL = {"edges_links": "edges_links.jsonl"}
RELEASE_PATHS = {
    "nodes": "brain/data/nodes.jsonl",
    "edges": "brain/data/edges.jsonl",
    "edges_links": "brain/data/edges_links.jsonl",
    "cells": "brain/data/cells.jsonl",
    "frontier": "brain/data/frontier.jsonl",
}
MANIFEST_NAMES = ("release.json", "release-manifest.json", "manifest.json")
MISSING_SOURCE = "<missing>"


class SemanticDiffError(ValueError):
    """An input is missing, malformed, ambiguous, or internally inconsistent."""


@dataclass(frozen=True)
class Snapshot:
    kind: str
    label: str
    artifacts: Mapping[str, Path]
    release_id: str | None = None

    def description(self) -> dict[str, str]:
        result = {"kind": self.kind, "path": self.label}
        if self.release_id is not None:
            result["release_id"] = self.release_id
        return result


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticDiffError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(raw: str) -> None:
    raise SemanticDiffError(f"non-finite JSON number {raw!r} is forbidden")


def _parse_json(text: str, location: str) -> Any:
    try:
        return contracts.parse_artifact_json_bytes(
            text.encode("utf-8"), location=location
        )
    except contracts.VerificationError as exc:
        raise SemanticDiffError(str(exc)) from exc


def _canonical(value: Any) -> str:
    try:
        return contracts._decimal_json(value).decode("utf-8")
    except contracts.VerificationError as exc:
        raise SemanticDiffError(f"value is not canonical artifact JSON: {exc}") from exc


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    return sorted(
        key
        for key in set(before) | set(after)
        if key not in before or key not in after or before[key] != after[key]
    )


def _require_string(row: Mapping[str, Any], key: str, location: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise SemanticDiffError(f"{location}: {key} must be a non-empty string")
    return value


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        handle = path.open("rb")
    except FileNotFoundError as exc:
        raise SemanticDiffError(f"missing Brain artifact: {path}") from exc

    saw_meta = False
    saw_data = False
    with handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SemanticDiffError(f"{path}:{line_number}: not valid UTF-8: {exc}") from exc
            row = _parse_json(line, f"{path}:{line_number}")
            if not isinstance(row, dict):
                raise SemanticDiffError(f"{path}:{line_number}: expected a JSON object")
            if "_meta" in row:
                if saw_meta or saw_data or set(row) != {"_meta"} or not isinstance(row["_meta"], dict):
                    raise SemanticDiffError(f"{path}:{line_number}: invalid metadata row")
                saw_meta = True
                continue
            if not saw_meta:
                raise SemanticDiffError(f"{path}:{line_number}: missing leading _meta object")
            saw_data = True
            yield line_number, row
    if not saw_meta:
        raise SemanticDiffError(f"{path}: empty file or missing leading _meta object")


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SemanticDiffError(
                        f"{path}:{line_number}: not valid UTF-8: {exc}"
                    ) from exc
                row = _parse_json(line, f"{path}:{line_number}")
                if not isinstance(row, dict) or set(row) != {"_meta"} or not isinstance(row["_meta"], dict):
                    raise SemanticDiffError(f"{path}:{line_number}: invalid metadata row")
                return row["_meta"]
    except FileNotFoundError as exc:
        raise SemanticDiffError(f"missing Brain artifact: {path}") from exc
    raise SemanticDiffError(f"{path}: empty file or missing leading _meta object")


def _resolve_manifest(path: Path) -> tuple[Path, Path]:
    if path.is_file():
        return path.resolve(strict=True), path.parent.resolve(strict=True)
    if not path.is_dir():
        raise SemanticDiffError(f"input does not exist or is not a file/directory: {path}")
    candidates = [path / name for name in MANIFEST_NAMES if (path / name).is_file()]
    if not candidates:
        raise SemanticDiffError(
            f"{path}: not a Brain data directory and no local release manifest found"
        )
    if len(candidates) != 1:
        names = ", ".join(candidate.name for candidate in candidates)
        raise SemanticDiffError(f"{path}: ambiguous local release manifests: {names}")
    return candidates[0].resolve(strict=True), path.resolve(strict=True)


def _resolve_snapshot(raw: Path) -> Snapshot:
    path = raw.expanduser()
    if path.is_dir():
        present = {key for key, name in DIRECT_REQUIRED.items() if (path / name).is_file()}
        if present == set(DIRECT_REQUIRED):
            root = path.resolve(strict=True)
            artifacts = {key: root / name for key, name in DIRECT_REQUIRED.items()}
            for key, name in DIRECT_OPTIONAL.items():
                candidate = root / name
                if candidate.is_file():
                    artifacts[key] = candidate
            return Snapshot("data-directory", str(root), artifacts)
        if present:
            missing = ", ".join(
                DIRECT_REQUIRED[key] for key in sorted(set(DIRECT_REQUIRED) - present)
            )
            raise SemanticDiffError(f"{path}: incomplete Brain data directory; missing {missing}")

    manifest_path, root = _resolve_manifest(path)
    try:
        document, _ = contracts.load_canonical_json(manifest_path)
        manifest = contracts.validate_release_manifest(document)
    except (OSError, contracts.VerificationError) as exc:
        raise SemanticDiffError(str(exc)) from exc

    by_path = {artifact["path"]: artifact for artifact in manifest["artifacts"]}
    artifacts: dict[str, Path] = {}
    for key, relative in RELEASE_PATHS.items():
        ref = by_path.get(relative)
        if ref is None:
            raise SemanticDiffError(
                f"{manifest_path}: release manifest is missing required artifact {relative}"
            )
        try:
            contracts.verify_file_ref(root, ref, f"$.artifacts[{relative!r}]")
            artifacts[key] = root.joinpath(*PurePosixPath(relative).parts)
        except (OSError, contracts.VerificationError) as exc:
            raise SemanticDiffError(str(exc)) from exc
    return Snapshot(
        "release-manifest",
        str(manifest_path),
        artifacts,
        release_id=manifest["release_id"],
    )


def _load_unique(path: Path, identity: str, kind: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line_number, row in _iter_jsonl(path):
        location = f"{path}:{line_number}"
        key = _require_string(row, identity, location)
        if key in result:
            raise SemanticDiffError(f"{location}: duplicate {kind} identity {key!r}")
        result[key] = row
    return result


def _compare_unique(
    before: Mapping[str, dict[str, Any]],
    after: Mapping[str, dict[str, Any]],
    identity: str,
) -> dict[str, list[dict[str, Any]]]:
    before_keys = set(before)
    after_keys = set(after)
    added = [after[key] for key in sorted(after_keys - before_keys)]
    removed = [before[key] for key in sorted(before_keys - after_keys)]
    changed = []
    for key in sorted(before_keys & after_keys):
        if before[key] != after[key]:
            changed.append({
                identity: key,
                "changed_fields": _changed_fields(before[key], after[key]),
                "before": before[key],
                "after": after[key],
            })
    return {"added": added, "removed": removed, "changed": changed}


def _row_variants(counter: Counter[str]) -> list[dict[str, Any]]:
    variants = []
    for encoded in sorted(counter):
        variants.append({
            "count": counter[encoded],
            "row": _parse_json(encoded, "edge variant"),
        })
    return variants


def _edge_identity(row: Mapping[str, Any], location: str) -> tuple[str, str, str]:
    src = _require_string(row, "src", location)
    dst = _require_string(row, "dst", location)
    kind = _require_string(row, "kind", location)
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        raise SemanticDiffError(f"{location}: provenance must be an object")
    return src, dst, kind


def _index_edges(connection: sqlite3.Connection, snapshot: Snapshot, side: str) -> None:
    pending: list[tuple[str, str, str, str, str, str, str, int]] = []
    for artifact in ("edges", "edges_links"):
        path = snapshot.artifacts.get(artifact)
        if path is None:
            continue
        for line_number, row in _iter_jsonl(path):
            location = f"{path}:{line_number}"
            src, dst, kind = _edge_identity(row, location)
            semantic = dict(row)
            semantic.pop("provenance", None)
            source = row["provenance"].get("source")
            pending.append((
                side,
                src,
                dst,
                kind,
                _canonical(row),
                _canonical(semantic),
                source if isinstance(source, str) and source else MISSING_SOURCE,
                1,
            ))
            if len(pending) >= 10_000:
                connection.executemany(
                    """INSERT INTO edge_variants
                       (side, src, dst, kind, full, semantic, source, count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(side, src, dst, kind, full)
                       DO UPDATE SET count = count + excluded.count""",
                    pending,
                )
                pending.clear()
    if pending:
        connection.executemany(
            """INSERT INTO edge_variants
               (side, src, dst, kind, full, semantic, source, count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(side, src, dst, kind, full)
               DO UPDATE SET count = count + excluded.count""",
            pending,
        )


def _db_variants(
    connection: sqlite3.Connection,
    side: str,
    identity: tuple[str, str, str],
) -> Counter[str]:
    return Counter({
        full: count
        for full, count in connection.execute(
            """SELECT full, count FROM edge_variants
               WHERE side = ? AND src = ? AND dst = ? AND kind = ?
               ORDER BY full""",
            (side, *identity),
        )
    })


def _non_provenance(counter: Counter[str]) -> Counter[str]:
    result: Counter[str] = Counter()
    for encoded, count in counter.items():
        row = _parse_json(encoded, "edge variant")
        row.pop("provenance", None)
        result[_canonical(row)] += count
    return result


def _edge_sources(counter: Counter[str]) -> Counter[str]:
    result: Counter[str] = Counter()
    for encoded, count in counter.items():
        source = _parse_json(encoded, "edge variant").get("provenance", {}).get("source")
        result[source if isinstance(source, str) and source else MISSING_SOURCE] += count
    return result


def _take_variants(counter: Counter[str], count: int) -> tuple[Counter[str], Counter[str]]:
    taken: Counter[str] = Counter()
    remainder = counter.copy()
    remaining = count
    for encoded in sorted(counter):
        amount = min(counter[encoded], remaining)
        if amount:
            taken[encoded] = amount
            remainder[encoded] -= amount
            if not remainder[encoded]:
                del remainder[encoded]
            remaining -= amount
        if not remaining:
            break
    return taken, remainder


def _variant_source(encoded: str) -> str:
    source = _parse_json(encoded, "edge variant").get("provenance", {}).get("source")
    return source if isinstance(source, str) and source else MISSING_SOURCE


def _variant_semantic(encoded: str) -> str:
    return next(iter(_non_provenance(Counter({encoded: 1}))))


def _pair_by_key(
    before: Counter[str],
    after: Counter[str],
    key_fn: Any,
) -> tuple[Counter[str], Counter[str], Counter[str], Counter[str]]:
    before_groups: dict[Any, Counter[str]] = defaultdict(Counter)
    after_groups: dict[Any, Counter[str]] = defaultdict(Counter)
    for encoded, count in before.items():
        before_groups[key_fn(encoded)][encoded] += count
    for encoded, count in after.items():
        after_groups[key_fn(encoded)][encoded] += count

    paired_before: Counter[str] = Counter()
    paired_after: Counter[str] = Counter()
    remaining_before = before.copy()
    remaining_after = after.copy()
    for key in sorted(set(before_groups) & set(after_groups)):
        pair_count = min(
            sum(before_groups[key].values()),
            sum(after_groups[key].values()),
        )
        taken_before, _ = _take_variants(before_groups[key], pair_count)
        taken_after, _ = _take_variants(after_groups[key], pair_count)
        paired_before.update(taken_before)
        paired_after.update(taken_after)
        remaining_before.subtract(taken_before)
        remaining_after.subtract(taken_after)
    return paired_before, paired_after, +remaining_before, +remaining_after


def _record_source_delta(
    grouped: dict[tuple[str, str], Counter[str]],
    kind: str,
    before: Counter[str],
    after: Counter[str],
    *,
    changed: bool = False,
) -> None:
    for source in sorted(set(before) | set(after)):
        removed = max(before[source] - after[source], 0)
        added = max(after[source] - before[source], 0)
        if removed:
            grouped[(source, kind)]["removed"] += removed
        if added:
            grouped[(source, kind)]["added"] += added
        if changed and before[source] and after[source]:
            grouped[(source, kind)]["changed"] += min(before[source], after[source])


def _identity_json(identity: tuple[str, str, str]) -> dict[str, str]:
    return {"src": identity[0], "dst": identity[1], "kind": identity[2]}


def _compare_edges(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="wikilean-semantic-diff-") as temp:
        connection = sqlite3.connect(Path(temp) / "edges.sqlite3")
        connection.executescript(
            """PRAGMA journal_mode = OFF;
               PRAGMA synchronous = OFF;
               CREATE TABLE edge_variants (
                 side TEXT NOT NULL,
                 src TEXT NOT NULL,
                 dst TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 full TEXT NOT NULL,
                 semantic TEXT NOT NULL,
                 source TEXT NOT NULL,
                 count INTEGER NOT NULL,
                 PRIMARY KEY (side, src, dst, kind, full)
               ) WITHOUT ROWID;
               CREATE INDEX edge_identity
                 ON edge_variants(src, dst, kind, side);"""
        )
        _index_edges(connection, before, "before")
        _index_edges(connection, after, "after")
        connection.commit()
        identities = connection.execute(
            "SELECT DISTINCT src, dst, kind FROM edge_variants ORDER BY src, dst, kind"
        )
        result = _compare_indexed_edges(connection, identities)
        connection.close()
        return result


def _compare_indexed_edges(
    connection: sqlite3.Connection,
    identities: Iterable[tuple[str, str, str]],
) -> dict[str, Any]:
    added = []
    removed = []
    changed = []
    provenance_only = []
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)

    for identity in identities:
        before_variants = _db_variants(connection, "before", identity)
        after_variants = _db_variants(connection, "after", identity)
        if not before_variants:
            added.append({**_identity_json(identity), "variants": _row_variants(after_variants)})
            for source, count in _edge_sources(after_variants).items():
                grouped[(source, identity[2])]["added"] += count
            continue
        if not after_variants:
            removed.append({**_identity_json(identity), "variants": _row_variants(before_variants)})
            for source, count in _edge_sources(before_variants).items():
                grouped[(source, identity[2])]["removed"] += count
            continue
        if before_variants == after_variants:
            continue
        shared = before_variants & after_variants
        unmatched_before = before_variants - shared
        unmatched_after = after_variants - shared
        same_source_before, same_source_after, remaining_before, remaining_after = (
            _pair_by_key(unmatched_before, unmatched_after, _variant_source)
        )
        if same_source_before:
            changed.append({
                **_identity_json(identity),
                "before": _row_variants(same_source_before),
                "after": _row_variants(same_source_after),
            })
            _record_source_delta(
                grouped,
                identity[2],
                _edge_sources(same_source_before),
                _edge_sources(same_source_after),
                changed=True,
            )
        provenance_before, provenance_after, semantic_before, semantic_after = _pair_by_key(
            remaining_before, remaining_after, _variant_semantic
        )
        if provenance_before:
            provenance_only.append({
                **_identity_json(identity),
                "before": _row_variants(provenance_before),
                "after": _row_variants(provenance_after),
            })
            _record_source_delta(
                grouped,
                identity[2],
                _edge_sources(provenance_before),
                _edge_sources(provenance_after),
                changed=True,
            )
        paired_count = min(sum(semantic_before.values()), sum(semantic_after.values()))
        paired_before, removed_variants = _take_variants(semantic_before, paired_count)
        paired_after, added_variants = _take_variants(semantic_after, paired_count)
        if paired_count:
            changed.append({
                **_identity_json(identity),
                "before": _row_variants(paired_before),
                "after": _row_variants(paired_after),
            })
            _record_source_delta(
                grouped,
                identity[2],
                _edge_sources(paired_before),
                _edge_sources(paired_after),
                changed=True,
            )
        if removed_variants:
            removed.append({
                **_identity_json(identity),
                "variants": _row_variants(removed_variants),
            })
            for source, count in _edge_sources(removed_variants).items():
                grouped[(source, identity[2])]["removed"] += count
        if added_variants:
            added.append({
                **_identity_json(identity),
                "variants": _row_variants(added_variants),
            })
            for source, count in _edge_sources(added_variants).items():
                grouped[(source, identity[2])]["added"] += count

    groups = [
        {
            "source": source,
            "kind": kind,
            "added": counts["added"],
            "removed": counts["removed"],
            "changed": counts["changed"],
        }
        for (source, kind), counts in sorted(grouped.items())
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "provenance_only": provenance_only,
        "grouped_by_source_kind": groups,
    }


def _decl_library(node_id: str) -> str:
    parts = node_id.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "decl" else MISSING_SOURCE


def _snippet_values(node: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    node_id = str(node.get("id", ""))
    node_type = node.get("type")
    values: list[tuple[str, str, Any]] = []
    if node_type == "decl":
        source = _decl_library(node_id)
        for field in ("code", "docstring", "slogan"):
            if field in node and node[field] is not None:
                values.append((f"declaration.{field}", source, node[field]))
    elif node_type == "ext":
        source = node.get("db") if isinstance(node.get("db"), str) else MISSING_SOURCE
        for field in ("snippet", "snippet_license"):
            if field in node and node[field] is not None:
                values.append((f"external.{field}", source, node[field]))
    elif node_type == "concept":
        unit = node.get("unit")
        description = unit.get("description") if isinstance(unit, dict) else None
        if description is None:
            description = node.get("description")
        if description is not None:
            values.append(("concept.description", "concept", description))
    return values


def _extract_snippets(nodes: Mapping[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for node_id in sorted(nodes):
        for field, source, value in _snippet_values(nodes[node_id]):
            result[(node_id, field)] = {
                "id": node_id,
                "field": field,
                "source": source,
                "value": value,
            }
    return result


def _compare_snippets(
    before: Mapping[str, dict[str, Any]],
    after: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    old = _extract_snippets(before)
    new = _extract_snippets(after)
    old_keys = set(old)
    new_keys = set(new)
    added = [new[key] for key in sorted(new_keys - old_keys)]
    removed = [old[key] for key in sorted(old_keys - new_keys)]
    changed = []
    for key in sorted(old_keys & new_keys):
        if old[key] != new[key]:
            changed.append({
                "id": key[0],
                "field": key[1],
                "before_source": old[key]["source"],
                "after_source": new[key]["source"],
                "before": old[key]["value"],
                "after": new[key]["value"],
            })
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for status, rows in (("added", added), ("removed", removed)):
        for row in rows:
            grouped[(row["field"], row["source"])][status] += 1
    for row in changed:
        before_source = row["before_source"]
        after_source = row["after_source"]
        if before_source == after_source:
            grouped[(row["field"], before_source)]["changed"] += 1
        else:
            grouped[(row["field"], before_source)]["removed"] += 1
            grouped[(row["field"], after_source)]["added"] += 1
    groups = [
        {
            "field": field,
            "source": source,
            "added": counts["added"],
            "removed": counts["removed"],
            "changed": counts["changed"],
        }
        for (field, source), counts in sorted(grouped.items())
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "grouped_by_field_source": groups,
    }


def _load_cells(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    cells: dict[str, dict[str, Any]] = {}
    organs: dict[str, dict[str, Any]] = {}
    provenance = _read_meta(path).get("prov")
    if provenance is not None and not isinstance(provenance, list):
        raise SemanticDiffError(f"{path}: _meta.prov must be an array")
    for line_number, row in _iter_jsonl(path):
        location = f"{path}:{line_number}"
        cell_id = _require_string(row, "id", location)
        if cell_id in cells:
            raise SemanticDiffError(f"{location}: duplicate cell identity {cell_id!r}")
        raw_organs = row.get("organs")
        if not isinstance(raw_organs, list):
            raise SemanticDiffError(f"{location}: organs must be an array")
        semantic_cell = {key: value for key, value in row.items() if key not in {"organs", "xy"}}
        cells[cell_id] = semantic_cell
        for index, organ in enumerate(raw_organs):
            organ_location = f"{location}.organs[{index}]"
            if not isinstance(organ, dict):
                raise SemanticDiffError(f"{organ_location}: expected an object")
            organ_id = _require_string(organ, "id", organ_location)
            payload = dict(organ)
            if "prov" in organ:
                index_value = organ["prov"]
                if isinstance(index_value, bool) or not isinstance(index_value, int):
                    raise SemanticDiffError(f"{organ_location}: prov must be an integer index")
                if not isinstance(provenance, list) or not 0 <= index_value < len(provenance):
                    raise SemanticDiffError(f"{organ_location}: prov index is outside _meta.prov")
                payload["provenance"] = provenance[index_value]
                payload.pop("prov")
            previous = organs.get(organ_id)
            if previous is not None:
                raise SemanticDiffError(
                    f"{organ_location}: organ {organ_id!r} has two owners: "
                    f"{previous['owner']!r} and {cell_id!r}"
                )
            organs[organ_id] = {"id": organ_id, "owner": cell_id, "payload": payload}
    return cells, organs


def _compare_organs(
    before: Mapping[str, dict[str, Any]],
    after: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    before_keys = set(before)
    after_keys = set(after)
    common = before_keys & after_keys
    added = [after[key] for key in sorted(after_keys - before_keys)]
    removed = [before[key] for key in sorted(before_keys - after_keys)]
    moved = []
    changed = []
    provenance_only = []
    for key in sorted(common):
        old = before[key]
        new = after[key]
        if old["owner"] != new["owner"]:
            moved.append({"id": key, "before": old["owner"], "after": new["owner"]})
        if old["payload"] != new["payload"]:
            record = {
                "id": key,
                "changed_fields": _changed_fields(old["payload"], new["payload"]),
                "before": old["payload"],
                "after": new["payload"],
            }
            old_semantic = {
                field: value for field, value in old["payload"].items()
                if field != "provenance"
            }
            new_semantic = {
                field: value for field, value in new["payload"].items()
                if field != "provenance"
            }
            if old_semantic == new_semantic:
                provenance_only.append(record)
            else:
                changed.append(record)

    old_to_new: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    new_from_old: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for organ_id in sorted(common):
        old_owner = before[organ_id]["owner"]
        new_owner = after[organ_id]["owner"]
        old_to_new[old_owner][new_owner].append(organ_id)
        new_from_old[new_owner][old_owner].append(organ_id)
    splits = [
        {
            "before": old_owner,
            "after": [
                {"cell": new_owner, "organs": destinations[new_owner]}
                for new_owner in sorted(destinations)
            ],
        }
        for old_owner, destinations in sorted(old_to_new.items())
        if len(destinations) > 1
    ]
    merges = [
        {
            "after": new_owner,
            "before": [
                {"cell": old_owner, "organs": sources[old_owner]}
                for old_owner in sorted(sources)
            ],
        }
        for new_owner, sources in sorted(new_from_old.items())
        if len(sources) > 1
    ]
    return {
        "added": added,
        "removed": removed,
        "moved": moved,
        "changed": changed,
        "provenance_only": provenance_only,
        "splits": splits,
        "merges": merges,
    }


def _normalize_frontier(row: dict[str, Any], location: str) -> dict[str, Any]:
    cells = row.get("cells")
    if not isinstance(cells, list) or not all(isinstance(cell, str) and cell for cell in cells):
        raise SemanticDiffError(f"{location}: cells must be an array of non-empty strings")
    if len(cells) != len(set(cells)):
        raise SemanticDiffError(f"{location}: cells contains duplicate identities")
    if "n" in row and row["n"] != len(cells):
        raise SemanticDiffError(f"{location}: n does not equal len(cells)")
    members = {cell: {} for cell in cells}
    for group_name in ("prox", "suitability"):
        group = row.get(group_name)
        if group is None:
            continue
        if not isinstance(group, dict):
            raise SemanticDiffError(f"{location}: {group_name} must be an object")
        for field, values in group.items():
            if not isinstance(values, list) or len(values) != len(cells):
                raise SemanticDiffError(
                    f"{location}: {group_name}.{field} must be an array parallel to cells"
                )
            for index, cell in enumerate(cells):
                members[cell].setdefault(group_name, {})[field] = values[index]
    area = {
        key: value
        for key, value in row.items()
        if key not in {"cells", "n", "prox", "suitability"}
    }
    return {
        "id": row["id"],
        "area": area,
        "members": {cell: members[cell] for cell in sorted(members)},
    }


def _load_frontier(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    owners: dict[str, str] = {}
    for line_number, row in _iter_jsonl(path):
        location = f"{path}:{line_number}"
        identity = _require_string(row, "id", location)
        if identity in result:
            raise SemanticDiffError(f"{location}: duplicate frontier identity {identity!r}")
        normalized = _normalize_frontier(row, location)
        for cell in normalized["members"]:
            previous = owners.setdefault(cell, identity)
            if previous != identity:
                raise SemanticDiffError(
                    f"{location}: frontier cell {cell!r} belongs to both "
                    f"{previous!r} and {identity!r}"
                )
        result[identity] = normalized
    return result


def _validate_generation(snapshot: Snapshot) -> None:
    meta = {name: _read_meta(path) for name, path in snapshot.artifacts.items()}
    base_names = [name for name in ("nodes", "edges", "edges_links") if name in meta]
    base_generations = {meta[name].get("generated_at") for name in base_names}
    base_snapshot_ids = {meta[name].get("snapshot_id") for name in base_names}
    if len(base_generations) != 1 or None in base_generations:
        raise SemanticDiffError(
            f"{snapshot.label}: organ graph artifacts have mixed or missing generated_at values"
        )
    present_snapshot_ids = {value for value in base_snapshot_ids if value is not None}
    if present_snapshot_ids and (
        len(present_snapshot_ids) != 1 or None in base_snapshot_ids
    ):
        raise SemanticDiffError(
            f"{snapshot.label}: organ graph artifacts have mixed snapshot_id values"
        )

    cell_generation = meta["cells"].get("generated_at")
    frontier_generation = meta["frontier"].get("generated_at")
    if cell_generation is None or frontier_generation is None or cell_generation != frontier_generation:
        raise SemanticDiffError(
            f"{snapshot.label}: cell/frontier artifacts have mixed or missing generated_at values"
        )
    base_generation = next(iter(base_generations))
    if meta["cells"].get("base_generated_at") not in (None, base_generation):
        raise SemanticDiffError(
            f"{snapshot.label}: cells.jsonl does not name the organ graph generated_at"
        )
    if present_snapshot_ids:
        base_snapshot_id = next(iter(present_snapshot_ids))
        if meta["cells"].get("base_snapshot_id") not in (None, base_snapshot_id):
            raise SemanticDiffError(
                f"{snapshot.label}: cells.jsonl does not name the organ graph snapshot_id"
            )


def _edge_record_count(record: Mapping[str, Any], status: str) -> int:
    variants = record.get("variants") if status in {"added", "removed"} else None
    if variants is not None:
        return sum(item["count"] for item in variants)
    before = sum(item["count"] for item in record.get("before", []))
    after = sum(item["count"] for item in record.get("after", []))
    return min(before, after)


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "nodes": {key: len(report["nodes"][key]) for key in ("added", "removed", "changed")},
        "edges": {
            key: sum(
                _edge_record_count(record, key)
                for record in report["edges"][key]
            )
            for key in ("added", "removed", "changed", "provenance_only")
        },
        "snippets": {
            key: len(report["snippets"][key]) for key in ("added", "removed", "changed")
        },
        "cells": {key: len(report["cells"][key]) for key in ("added", "removed", "changed")},
        "organ_membership": {
            key: len(report["organ_membership"][key])
            for key in (
                "added", "removed", "moved", "changed", "provenance_only",
                "splits", "merges",
            )
        },
        "frontier": {
            key: len(report["frontier"][key]) for key in ("added", "removed", "changed")
        },
    }


def _has_differences(summary: Mapping[str, Any]) -> bool:
    return any(
        count
        for section in summary.values()
        for count in section.values()
    )


def compare(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    # Parse content before checking cross-artifact generation pins. This reports a
    # malformed artifact directly instead of masking it behind metadata read from
    # the remaining files; generation consistency is still required before a
    # report can be emitted.
    before_nodes = _load_unique(before.artifacts["nodes"], "id", "node")
    after_nodes = _load_unique(after.artifacts["nodes"], "id", "node")
    before_cells, before_organs = _load_cells(before.artifacts["cells"])
    after_cells, after_organs = _load_cells(after.artifacts["cells"])
    before_frontier = _load_frontier(before.artifacts["frontier"])
    after_frontier = _load_frontier(after.artifacts["frontier"])
    edges = _compare_edges(before, after)
    _validate_generation(before)
    _validate_generation(after)

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "from": before.description(),
        "to": after.description(),
        "nodes": _compare_unique(before_nodes, after_nodes, "id"),
        "edges": edges,
        "snippets": _compare_snippets(before_nodes, after_nodes),
        "cells": _compare_unique(before_cells, after_cells, "id"),
        "organ_membership": _compare_organs(before_organs, after_organs),
        "frontier": _compare_unique(before_frontier, after_frontier, "id"),
    }
    report["summary"] = _summary(report)
    report["different"] = _has_differences(report["summary"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="before", type=Path, required=True)
    parser.add_argument("--to", dest="after", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare(_resolve_snapshot(args.before), _resolve_snapshot(args.after))
    except (OSError, SemanticDiffError) as exc:
        print(f"semantic_diff: {exc}", file=sys.stderr)
        return 2
    print(_canonical(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
