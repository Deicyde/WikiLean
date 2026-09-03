#!/usr/bin/env python3
"""Read and build the local BRAIN snapshot.

JSONL remains the committed interchange format. ``brain.sqlite3`` is a generated,
read-only index over the same rows. All indexed records retain their complete
canonical JSON payload, and ordinals preserve JSONL order.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
APPLICATION_ID = 0x574C424E  # "WLBN"
DEFAULT_SQLITE_NAME = "brain.sqlite3"
DEFAULT_ARTIFACT_FILES = {
    "nodes": "nodes.jsonl",
    "edges": "edges.jsonl",
    "edges_links": "edges_links.jsonl",
    "cells": "cells.jsonl",
    "synapses": "synapses.jsonl",
}


class StoreError(RuntimeError):
    """The selected store is missing, malformed, incomplete, or stale."""


class StaleSnapshotError(StoreError):
    """The generated SQLite snapshot does not match adjacent JSONL."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def digest_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json(dict(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_metadata(path: Path, *, optional: bool = False) -> dict[str, Any]:
    if optional and not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("_meta"), dict):
                    return row["_meta"]
                raise StoreError(f"{path} has no first-line _meta object")
    except FileNotFoundError as exc:
        raise StoreError(f"missing Brain artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path}:1: invalid metadata JSON: {exc}") from exc
    raise StoreError(f"{path} is empty")


def iter_jsonl(path: Path, *, optional: bool = False) -> Iterator[dict[str, Any]]:
    if optional and not path.exists():
        return
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StoreError(f"missing Brain artifact: {path}") from exc
    with handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise StoreError(f"{path}:{lineno}: expected a JSON object")
            if "_meta" not in row:
                yield row


def _handle_metadata(handle, path: Path) -> dict[str, Any]:
    handle.seek(0)
    for line in handle:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path}:1: invalid metadata JSON: {exc}") from exc
        if isinstance(row, dict) and isinstance(row.get("_meta"), dict):
            return row["_meta"]
        raise StoreError(f"{path} has no first-line _meta object")
    raise StoreError(f"{path} is empty")


def _iter_handle(
    handle, path: Path, *, raw_hasher: Any | None = None
) -> Iterator[dict[str, Any]]:
    handle.seek(0)
    for lineno, line in enumerate(handle, 1):
        if raw_hasher is not None:
            raw_hasher.update(line)
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise StoreError(f"{path}:{lineno}: expected a JSON object")
        if "_meta" not in row:
            yield row


def read_jsonl(
    path: Path, *, optional: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if optional and not path.exists():
        return {}, []
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StoreError(f"missing Brain artifact: {path}") from exc
    meta: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    with handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise StoreError(f"{path}:{lineno}: expected a JSON object")
            if "_meta" in row:
                if (
                    meta is not None
                    or lineno != 1
                    or not isinstance(row["_meta"], dict)
                ):
                    raise StoreError(f"{path}:{lineno}: invalid metadata row")
                meta = row["_meta"]
            else:
                rows.append(row)
    if meta is None:
        raise StoreError(f"{path} has no first-line _meta object")
    return meta, rows


def _paths(
    data_dir: Path,
    artifact_paths: Mapping[str, str | os.PathLike[str]] | None = None,
) -> dict[str, Path]:
    out = {
        name: data_dir / filename for name, filename in DEFAULT_ARTIFACT_FILES.items()
    }
    if artifact_paths:
        out.update({name: Path(value) for name, value in artifact_paths.items()})
    return out


def load_artifacts(
    data_dir: Path,
    artifact_paths: Mapping[str, str | os.PathLike[str]] | None = None,
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    paths = _paths(data_dir, artifact_paths)
    artifacts = {
        "nodes": read_jsonl(paths["nodes"]),
        "edges": read_jsonl(paths["edges"]),
        "edges_links": read_jsonl(paths["edges_links"], optional=True),
    }
    if paths["cells"].exists() or paths["synapses"].exists():
        artifacts["cells"] = read_jsonl(paths["cells"])
        artifacts["synapses"] = read_jsonl(paths["synapses"])
    validate_generations(artifacts)
    return artifacts


def validate_generations(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
) -> None:
    def generation(name: str) -> str | None:
        item = artifacts.get(name)
        return (
            str(item[0].get("generated_at"))
            if item and item[0].get("generated_at")
            else None
        )

    base = generation("nodes")
    if base is None:
        raise StoreError("Brain base artifacts have no generated_at version pin")
    if base != generation("edges"):
        raise StoreError("nodes.jsonl and edges.jsonl belong to different generations")
    if artifacts.get("edges_links", ({}, []))[0] and base != generation("edges_links"):
        raise StoreError(
            "edges.jsonl and edges_links.jsonl belong to different generations"
        )
    if "cells" in artifacts and generation("cells") != generation("synapses"):
        raise StoreError(
            "cells.jsonl and synapses.jsonl belong to different generations"
        )
    if "cells" in artifacts:
        cell_meta, synapse_meta = artifacts["cells"][0], artifacts["synapses"][0]
        if cell_meta.get("base_generated_at") and cell_meta["base_generated_at"] != base:
            raise StoreError("cell layer derives from a different base generation")
        if cell_meta.get("base_generated_at") != synapse_meta.get("base_generated_at"):
            raise StoreError("cells.jsonl and synapses.jsonl have different base parents")
        if cell_meta.get("base_snapshot_id") != synapse_meta.get("base_snapshot_id"):
            raise StoreError("cells.jsonl and synapses.jsonl have different base snapshot parents")
    base_names = ["nodes", "edges"]
    if artifacts.get("edges_links", ({}, []))[0]:
        base_names.append("edges_links")
    base_ids = {
        artifacts[name][0].get("snapshot_id")
        for name in base_names
        if artifacts[name][0].get("snapshot_id")
    }
    if len(base_ids) > 1:
        raise StoreError("Brain base artifacts have different snapshot IDs")
    if base_ids and any(not artifacts[name][0].get("snapshot_id") for name in base_names):
        raise StoreError("Brain base artifacts mix snapshot-aware and legacy generations")
    if base_ids and "cells" in artifacts:
        base_id = next(iter(base_ids))
        if artifacts["cells"][0].get("base_snapshot_id") != base_id:
            raise StoreError("cell layer is not pinned to the current base snapshot")


def artifact_digests(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, (meta, rows) in artifacts.items():
        out[name] = _artifact_digest(meta, digest_rows(rows))
    return out


def _artifact_digest(meta: Mapping[str, Any], rows_digest: str) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(dict(meta)).encode("utf-8"))
    digest.update(b"\n")
    digest.update(rows_digest.encode("ascii"))
    return digest.hexdigest()


def _identity_from_digests(
    domain: str,
    names: Iterable[str],
    digests: Mapping[str, str],
) -> str:
    """Return a domain-separated identity over ordered logical artifacts."""
    preimage = {
        "domain": domain,
        "artifacts": [[name, digests[name]] for name in names],
    }
    return hashlib.sha256(canonical_json(preimage).encode("utf-8")).hexdigest()


def base_snapshot_id_for(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
    digests: Mapping[str, str] | None = None,
) -> str:
    """Return the published organ snapshot ID or a legacy logical equivalent."""
    names = ("nodes", "edges", "edges_links")
    published = {
        artifacts[name][0].get("snapshot_id")
        for name in names
        if artifacts[name][0].get("snapshot_id")
    }
    if published:
        # validate_generations() rejects mixed or partially populated IDs.
        return str(next(iter(published)))
    logical = dict(digests or artifact_digests(artifacts))
    return _identity_from_digests("wikilean-brain-base-v1", names, logical)


def projection_id_for(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
    digests: Mapping[str, str] | None = None,
) -> str:
    """Identify the complete logical SQLite projection, including derived layers."""
    logical = dict(digests or artifact_digests(artifacts))
    names = tuple(
        name
        for name in ("nodes", "edges", "edges_links", "cells", "synapses")
        if name in artifacts
    )
    return _identity_from_digests("wikilean-brain-projection-v1", names, logical)


def snapshot_id_for(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
) -> str:
    """Backward-compatible name for the base organ snapshot identity."""
    return base_snapshot_id_for(artifacts)


def _owner_fields(
    organ_id: str, organ: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Return the indexed kind and bare declaration name for one organ."""
    kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
    bare = (
        organ_id.split(":", 2)[2]
        if kind == "decl" and organ_id.count(":") >= 2
        else None
    )
    return kind, bare


def _derive_owners(
    cells: Iterable[dict[str, Any]], cell_meta: Mapping[str, Any]
) -> dict[str, tuple[str, str | None, str | None]]:
    owners: dict[str, tuple[str, str | None, str | None]] = {}

    def claim(organ: Mapping[str, Any], owner: str) -> None:
        organ_id = organ.get("id")
        if not isinstance(organ_id, str):
            return
        kind, bare = _owner_fields(organ_id, organ)
        previous = owners.setdefault(organ_id, (owner, kind, bare))
        if previous[0] != owner:
            raise StoreError(
                f"organ {organ_id!r} has two owners: {previous[0]!r} and {owner!r}"
            )

    for cell in cells:
        owner = cell.get("id")
        if not isinstance(owner, str):
            raise StoreError("cell row is missing a string id")
        for organ in cell.get("organs") or []:
            claim(organ, owner)
    for owner, organs in (cell_meta.get("supercell_organs") or {}).items():
        for organ in organs:
            organ_id = organ.get("id")
            # Rule 5 may list a concept on a supercell even when an exact bond
            # already gave it a cell. The cell remains the canonical owner.
            if isinstance(organ_id, str) and organ_id not in owners:
                claim(organ, owner)
    return owners


_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE snapshot (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_version INTEGER NOT NULL,
  build_state TEXT NOT NULL CHECK (build_state IN ('building','complete')),
  snapshot_id TEXT NOT NULL,
  base_snapshot_id TEXT NOT NULL,
  projection_id TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  CHECK (snapshot_id = base_snapshot_id)
);
CREATE TABLE artifacts (
  name TEXT PRIMARY KEY,
  generated_at TEXT,
  row_count INTEGER NOT NULL,
  digest TEXT NOT NULL,
  source_digest TEXT,
  logical_digest TEXT NOT NULL,
  raw_digest TEXT,
  source_present INTEGER NOT NULL CHECK (source_present IN (0, 1)),
  metadata_json TEXT NOT NULL,
  CHECK (digest = logical_digest),
  CHECK (source_digest IS raw_digest),
  CHECK (
    (source_present = 1 AND raw_digest IS NOT NULL) OR
    (source_present = 0 AND raw_digest IS NULL)
  )
) WITHOUT ROWID;
CREATE TABLE nodes (
  ordinal INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  label TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX nodes_type_label_idx ON nodes(type, label);
CREATE TABLE edges (
  stream TEXT NOT NULL CHECK (stream IN ('main','links')),
  ordinal INTEGER NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL,
  confidence TEXT,
  provenance_source TEXT,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (stream, ordinal)
) WITHOUT ROWID;
CREATE INDEX edges_src_kind_idx ON edges(src, kind);
CREATE INDEX edges_dst_kind_idx ON edges(dst, kind);
CREATE INDEX edges_kind_stream_idx ON edges(kind, stream);
CREATE TABLE cells (
  ordinal INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  anchor TEXT,
  label TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX cells_label_idx ON cells(label);
CREATE TABLE organ_owners (
  organ_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  organ_kind TEXT,
  bare_decl TEXT
) WITHOUT ROWID;
CREATE INDEX organ_owners_owner_idx ON organ_owners(owner_id);
CREATE INDEX organ_owners_bare_decl_idx ON organ_owners(bare_decl);
CREATE TABLE synapses (
  ordinal INTEGER PRIMARY KEY,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  weight INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(src, dst)
);
CREATE INDEX synapses_src_idx ON synapses(src);
CREATE INDEX synapses_dst_idx ON synapses(dst);
"""


def _publish_sqlite(temp: Path, target: Path) -> None:
    """Durably publish a completed database and its directory entry."""
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, target)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(target.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_sqlite_snapshot(
    path: str | os.PathLike[str],
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
    *,
    source_digests: Mapping[str, str | None] | None = None,
) -> Path:
    """Build and atomically publish a complete SQLite projection.

    ``source_digests`` binds rows to exact adjacent JSONL bytes. Omitting an
    entry records that no raw source was supplied, so freshness-checked readers
    will reject that projection beside a present source file.
    """
    validate_generations(artifacts)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    digests = artifact_digests(artifacts)
    base_snapshot_id = base_snapshot_id_for(artifacts, digests)
    projection_id = projection_id_for(artifacts, digests)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(_SCHEMA_SQL)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        base_meta = dict(artifacts["nodes"][0])
        connection.execute(
            "INSERT INTO snapshot VALUES (1, ?, 'building', ?, ?, ?, ?)",
            (
                SCHEMA_VERSION,
                base_snapshot_id,
                base_snapshot_id,
                projection_id,
                canonical_json(base_meta),
            ),
        )
        for name, (meta, rows) in artifacts.items():
            raw_digest = (source_digests or {}).get(name)
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    meta.get("generated_at"),
                    len(rows),
                    digests[name],
                    raw_digest,
                    digests[name],
                    raw_digest,
                    int(raw_digest is not None),
                    canonical_json(dict(meta)),
                ),
            )
        connection.executemany(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
            [
                (i, row["id"], row["type"], row.get("label"), canonical_json(row))
                for i, row in enumerate(artifacts["nodes"][1])
            ],
        )
        edge_records = []
        for stream, artifact in (("main", "edges"), ("links", "edges_links")):
            for i, row in enumerate(artifacts[artifact][1]):
                provenance = row.get("provenance") or {}
                edge_records.append(
                    (
                        stream,
                        i,
                        row["src"],
                        row["dst"],
                        row["kind"],
                        row.get("confidence"),
                        provenance.get("source"),
                        canonical_json(row),
                    )
                )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)", edge_records
        )
        if "cells" in artifacts:
            cells_meta, cells = artifacts["cells"]
            connection.executemany(
                "INSERT INTO cells VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        i,
                        row["id"],
                        row.get("anchor"),
                        row.get("label"),
                        canonical_json(row),
                    )
                    for i, row in enumerate(cells)
                ],
            )
            owners = _derive_owners(cells, cells_meta)
            connection.executemany(
                "INSERT INTO organ_owners VALUES (?, ?, ?, ?)",
                [
                    (organ, owner, kind, bare)
                    for organ, (owner, kind, bare) in sorted(owners.items())
                ],
            )
            connection.executemany(
                "INSERT INTO synapses VALUES (?, ?, ?, ?, ?)",
                [
                    (i, row["src"], row["dst"], row["weight"], canonical_json(row))
                    for i, row in enumerate(artifacts["synapses"][1])
                ],
            )
        connection.execute("ANALYZE")
        connection.execute("UPDATE snapshot SET build_state = 'complete' WHERE singleton = 1")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StoreError("SQLite integrity_check failed")
        connection.close()
        connection = None
        _publish_sqlite(temp, target)
        return target
    except (KeyError, sqlite3.Error) as exc:
        raise StoreError(f"could not build SQLite snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)


def _inspect_source_handle(
    handle, path: Path
) -> tuple[dict[str, Any], int, str, str]:
    """Inspect metadata, row count, raw bytes, and logical rows in one pass."""
    raw = hashlib.sha256()
    rows = hashlib.sha256()
    metadata: dict[str, Any] | None = None
    count = 0
    handle.seek(0)
    for lineno, line in enumerate(handle, 1):
        raw.update(line)
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise StoreError(f"{path}:{lineno}: expected a JSON object")
        if "_meta" in row:
            if (
                metadata is not None
                or lineno != 1
                or not isinstance(row["_meta"], dict)
            ):
                raise StoreError(f"{path}:{lineno}: invalid metadata row")
            metadata = row["_meta"]
            continue
        count += 1
        rows.update(canonical_json(row).encode("utf-8"))
        rows.update(b"\n")
    if metadata is None:
        raise StoreError(f"{path} has no first-line _meta object")
    return metadata, count, raw.hexdigest(), _artifact_digest(metadata, rows.hexdigest())


def _open_regular_source(path: Path, *, required: bool) -> Any | None:
    """Open one source without following its final path component."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        if required:
            raise StoreError(f"missing Brain artifact: {path}") from exc
        return None
    except OSError as exc:
        raise StoreError(f"could not securely open Brain artifact {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise StoreError(f"Brain artifact is not a regular file: {path}")
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not os.path.samestat(opened, current):
            raise StoreError(f"Brain artifact is a symlink or changed while opening: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _source_handles(
    paths: Mapping[str, Path],
    *,
    required_artifacts: Iterable[str] | None = None,
) -> dict[str, Any | None]:
    """Open one immutable view of all source paths, including optional layers.

    ``required_artifacts`` enables the sealed-replay path: every named artifact
    is mandatory and is opened as a regular file without following a final
    symlink.  Omitting it preserves the legacy optional links/cell behavior.
    """
    strict = required_artifacts is not None
    required = set(required_artifacts or ())
    known = set(DEFAULT_ARTIFACT_FILES)
    unknown = sorted(required - known)
    if unknown:
        raise StoreError("unknown required Brain artifacts: " + ", ".join(unknown))
    if strict and not {"nodes", "edges"} <= required:
        raise StoreError("nodes and edges must be required Brain artifacts")
    if strict and (("cells" in required) != ("synapses" in required)):
        raise StoreError("cells and synapses must be required together")

    handles: dict[str, Any | None] = {}
    try:
        if strict:
            for name in DEFAULT_ARTIFACT_FILES:
                handles[name] = _open_regular_source(
                    paths[name], required=name in required
                )
            return handles

        for name in ("nodes", "edges"):
            try:
                handles[name] = paths[name].open("rb")
            except FileNotFoundError as exc:
                raise StoreError(f"missing Brain artifact: {paths[name]}") from exc
        try:
            handles["edges_links"] = paths["edges_links"].open("rb")
        except FileNotFoundError:
            handles["edges_links"] = None

        derived = []
        for name in ("cells", "synapses"):
            try:
                handles[name] = paths[name].open("rb")
                derived.append(name)
            except FileNotFoundError:
                handles[name] = None
        if len(derived) == 1:
            raise StoreError("Brain cell JSONL generation is incomplete")
        return handles
    except Exception:
        for handle in handles.values():
            if handle is not None:
                handle.close()
        raise


def _close_source_handles(handles: Mapping[str, Any | None]) -> None:
    """Attempt every close and report the first failure after all handles."""
    errors: list[BaseException] = []
    for handle in handles.values():
        if handle is None:
            continue
        try:
            handle.close()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def _sources_unchanged(
    paths: Mapping[str, Path],
    handles: Mapping[str, Any | None],
    signatures: Mapping[str, tuple[int, int, int] | None],
    *,
    reject_symlinks: bool = False,
) -> None:
    """Fail a build if a pinned source was replaced or changed in place."""
    for name, handle in handles.items():
        if handle is None:
            if paths[name].exists():
                raise StoreError(f"{paths[name]} appeared while building SQLite")
            continue
        try:
            path_stat = paths[name].lstat() if reject_symlinks else paths[name].stat()
        except FileNotFoundError as exc:
            raise StoreError(f"{paths[name]} disappeared while building SQLite") from exc
        if reject_symlinks and (
            stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode)
        ):
            raise StoreError(f"{paths[name]} is no longer a regular non-symlink file")
        if not os.path.samestat(os.fstat(handle.fileno()), path_stat):
            raise StoreError(f"{paths[name]} was replaced while building SQLite")
        current = os.fstat(handle.fileno())
        signature = (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        if signature != signatures[name]:
            raise StoreError(f"{paths[name]} changed while building SQLite")


def write_sqlite_from_jsonl(
    path: Path,
    data_dir: Path,
    *,
    artifact_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    required_artifacts: Iterable[str] | None = None,
    temp_dir: Path | None = None,
    publisher: Callable[[Path, Path], None] | None = None,
) -> Path:
    """Stream one pinned, self-consistent JSONL generation into SQLite.

    The optional keyword arguments are for sealed replay.  They bind every
    logical artifact to an exact path, make selected artifacts mandatory, and
    keep temporary SQLite files inside stage-owned scratch space.  A custom
    publisher can enforce the caller's output ownership policy.
    """
    paths = _paths(data_dir, artifact_paths)
    handles: dict[str, Any | None] = {}
    target = Path(path)
    temp: Path | None = None
    connection: sqlite3.Connection | None = None
    try:
        handles = _source_handles(paths, required_artifacts=required_artifacts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_parent = Path(temp_dir) if temp_dir is not None else target.parent
        temp_parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=temp_parent
        )
        temp = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        inspected: dict[str, tuple[dict[str, Any], int, str | None, str]] = {}
        for name, handle in handles.items():
            if handle is None:
                empty_rows = hashlib.sha256().hexdigest()
                inspected[name] = ({}, 0, None, _artifact_digest({}, empty_rows))
            else:
                inspected[name] = _inspect_source_handle(handle, paths[name])

        names = ["nodes", "edges", "edges_links"]
        if handles["cells"] is not None:
            names.extend(("cells", "synapses"))
        metadata = {name: inspected[name][0] for name in names}
        counts = {name: inspected[name][1] for name in names}
        raw_digests = {name: inspected[name][2] for name in names}
        logical_digests = {name: inspected[name][3] for name in names}
        signatures = {
            name: (
                None
                if handles[name] is None
                else (
                    os.fstat(handles[name].fileno()).st_size,
                    os.fstat(handles[name].fileno()).st_mtime_ns,
                    os.fstat(handles[name].fileno()).st_ctime_ns,
                )
            )
            for name in handles
        }
        identity_artifacts = {
            name: (metadata[name], [])
            for name in names
        }
        validate_generations(identity_artifacts)
        base_snapshot_id = base_snapshot_id_for(
            identity_artifacts, logical_digests
        )
        projection_id = projection_id_for(identity_artifacts, logical_digests)

        connection = sqlite3.connect(temp)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(_SCHEMA_SQL)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO snapshot VALUES (1, ?, 'building', ?, ?, ?, ?)",
            (
                SCHEMA_VERSION,
                base_snapshot_id,
                base_snapshot_id,
                projection_id,
                canonical_json(metadata["nodes"]),
            ),
        )

        imported_digests: dict[str, str] = {}

        def tracked_rows(name: str) -> Iterator[dict[str, Any]]:
            rows = hashlib.sha256()
            raw = hashlib.sha256()
            count = 0
            handle = handles[name]
            if handle is not None:
                for count, row in enumerate(
                    _iter_handle(handle, paths[name], raw_hasher=raw), 1
                ):
                    rows.update(canonical_json(row).encode("utf-8"))
                    rows.update(b"\n")
                    yield row
            if count != counts[name]:
                raise StoreError(f"{paths[name]} changed row count while building SQLite")
            imported_digests[name] = _artifact_digest(
                metadata[name], rows.hexdigest()
            )
            if handle is not None and raw.hexdigest() != raw_digests[name]:
                raise StoreError(f"{paths[name]} changed while building SQLite")

        connection.executemany(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
            (
                (i, row["id"], row["type"], row.get("label"), canonical_json(row))
                for i, row in enumerate(tracked_rows("nodes"))
            ),
        )

        for stream, name in (("main", "edges"), ("links", "edges_links")):
            def insert_edges(
                stream: str = stream, name: str = name
            ) -> Iterator[tuple[Any, ...]]:
                for i, row in enumerate(tracked_rows(name)):
                    provenance = row.get("provenance") or {}
                    yield (
                        stream,
                        i,
                        row["src"],
                        row["dst"],
                        row["kind"],
                        row.get("confidence"),
                        provenance.get("source"),
                        canonical_json(row),
                    )

            connection.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)", insert_edges()
            )

        if "cells" in metadata:
            owners: dict[str, tuple[str, str | None, str | None]] = {}

            def insert_cells() -> Iterator[tuple[Any, ...]]:
                for i, row in enumerate(tracked_rows("cells")):
                    owner = row["id"]
                    for organ in row.get("organs") or []:
                        organ_id = organ.get("id")
                        if not isinstance(organ_id, str):
                            continue
                        kind, bare = _owner_fields(organ_id, organ)
                        previous = owners.setdefault(
                            organ_id, (owner, kind, bare)
                        )
                        if previous[0] != owner:
                            raise StoreError(
                                f"organ {organ_id!r} has two owners: "
                                f"{previous[0]!r} and {owner!r}"
                            )
                    yield (
                        i,
                        owner,
                        row.get("anchor"),
                        row.get("label"),
                        canonical_json(row),
                    )

            connection.executemany(
                "INSERT INTO cells VALUES (?, ?, ?, ?, ?)",
                insert_cells(),
            )

            # Cell ownership wins over a supercell listing for the same concept.
            for owner, organs in (
                metadata["cells"].get("supercell_organs") or {}
            ).items():
                for organ in organs:
                    organ_id = organ.get("id")
                    if not isinstance(organ_id, str):
                        continue
                    kind, bare = _owner_fields(organ_id, organ)
                    owners.setdefault(
                        organ_id, (owner, kind, bare)
                    )
            connection.executemany(
                "INSERT INTO organ_owners VALUES (?, ?, ?, ?)",
                [
                    (organ, owner, kind, bare)
                    for organ, (owner, kind, bare) in sorted(owners.items())
                ],
            )

            connection.executemany(
                "INSERT INTO synapses VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        i,
                        row["src"],
                        row["dst"],
                        row["weight"],
                        canonical_json(row),
                    )
                    for i, row in enumerate(tracked_rows("synapses"))
                ),
            )

        if imported_digests != logical_digests:
            raise StoreError("Brain JSONL logical content changed while building SQLite")
        _sources_unchanged(
            paths,
            handles,
            signatures,
            reject_symlinks=required_artifacts is not None,
        )

        for name, meta in metadata.items():
            raw_digest = raw_digests[name]
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    meta.get("generated_at"),
                    counts[name],
                    logical_digests[name],
                    raw_digest,
                    logical_digests[name],
                    raw_digest,
                    int(raw_digest is not None),
                    canonical_json(meta),
                ),
            )
        connection.execute("ANALYZE")
        connection.execute(
            "UPDATE snapshot SET build_state = 'complete' WHERE singleton = 1"
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StoreError("SQLite integrity_check failed")
        connection.close()
        connection = None
        _sources_unchanged(
            paths,
            handles,
            signatures,
            reject_symlinks=required_artifacts is not None,
        )
        try:
            _close_source_handles(handles)
        finally:
            handles = {}
        if publisher is None:
            _publish_sqlite(temp, target)
        else:
            publisher(temp, target)
        return target
    except (KeyError, sqlite3.Error) as exc:
        raise StoreError(f"could not import JSONL snapshot: {exc}") from exc
    finally:
        cleanup_errors: list[BaseException] = []
        if connection is not None:
            try:
                connection.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            _close_source_handles(handles)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            active = sys.exception()
            if active is not None and hasattr(active, "add_note"):
                active.add_note(
                    "SQLite cleanup also failed: "
                    + "; ".join(str(error) for error in cleanup_errors)
                )
            else:
                raise cleanup_errors[0]


class JsonlStore:
    backend = "jsonl"

    def __init__(self, data_dir: Path, artifact_paths=None) -> None:
        self.data_dir = data_dir
        self.paths = _paths(data_dir, artifact_paths)
        self._handles = {}
        try:
            for name in ("nodes", "edges"):
                self._handles[name] = self.paths[name].open(encoding="utf-8")
            if self.paths["edges_links"].exists():
                self._handles["edges_links"] = self.paths["edges_links"].open(encoding="utf-8")
            if self.paths["cells"].exists() or self.paths["synapses"].exists():
                if not self.paths["cells"].exists() or not self.paths["synapses"].exists():
                    raise StoreError("Brain cell JSONL generation is incomplete")
                self._handles["cells"] = self.paths["cells"].open(encoding="utf-8")
                self._handles["synapses"] = self.paths["synapses"].open(encoding="utf-8")
            self._metadata = {
                name: _handle_metadata(handle, self.paths[name])
                for name, handle in self._handles.items()
            }
            self._metadata.setdefault("edges_links", {})
            validate_generations({name: (meta, []) for name, meta in self._metadata.items()})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def metadata(self, artifact: str | None = None) -> dict[str, Any]:
        if artifact is None:
            return {name: dict(meta) for name, meta in self._metadata.items()}
        return dict(self._metadata.get(artifact, {}))

    get_metadata = metadata

    def iter_nodes(self) -> Iterator[dict[str, Any]]:
        yield from _iter_handle(self._handles["nodes"], self.paths["nodes"])

    def iter_edges(self, endpoint=None, direction="both", kinds=None, stream=None):
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        names = (
            ["edges", "edges_links"]
            if stream is None
            else ["edges_links" if stream == "links" else "edges"]
        )
        for name in names:
            handle = self._handles.get(name)
            if handle is None:
                continue
            for row in _iter_handle(handle, self.paths[name]):
                if kinds and row.get("kind") not in kinds:
                    continue
                if endpoint is not None:
                    if direction == "out" and row.get("src") != endpoint:
                        continue
                    if direction == "in" and row.get("dst") != endpoint:
                        continue
                    if direction == "both" and endpoint not in (
                        row.get("src"),
                        row.get("dst"),
                    ):
                        continue
                yield row

    def iter_cells(self):
        handle = self._handles.get("cells")
        if handle is not None:
            yield from _iter_handle(handle, self.paths["cells"])

    def resolve_owner(self, key: str) -> str | None:
        if key.startswith(("cell:", "path:")):
            return key
        cells_meta = self._metadata.get("cells", {})
        cells = list(self.iter_cells())
        owners = _derive_owners(cells, cells_meta)
        if key in owners:
            return owners[key][0]
        bare = (
            key.split(":", 2)[2]
            if key.startswith("decl:") and key.count(":") >= 2
            else key
        )
        for _, (owner, kind, decl_bare) in owners.items():
            if kind == "decl" and decl_bare == bare:
                return owner
        for cell in cells:
            if cell.get("label") == key:
                return cell["id"]
        return None

    def iter_synapses(self, endpoint=None, kinds=None):
        handle = self._handles.get("synapses")
        if handle is None:
            return
        for row in _iter_handle(handle, self.paths["synapses"]):
            if endpoint is not None and endpoint not in (
                row.get("src"),
                row.get("dst"),
            ):
                continue
            if kinds and not (set(row.get("kinds") or {}) & set(kinds)):
                continue
            if kinds:
                yield {
                    **row,
                    "traces": [
                        t for t in row.get("traces", []) if t.get("kind") in kinds
                    ],
                }
            else:
                yield row


class SQLiteStore:
    backend = "sqlite"

    def __init__(self, path: Path, data_dir: Path, artifact_paths=None,
        *, require_derived: bool = False) -> None:
        if not path.exists():
            raise StoreError(f"SQLite Brain snapshot is absent: {path}")
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection: sqlite3.Connection | None = None
        try:
            self.connection = sqlite3.connect(uri, uri=True)
            application_id = self.connection.execute(
                "PRAGMA application_id"
            ).fetchone()[0]
            if application_id != APPLICATION_ID:
                raise StoreError(
                    f"not a Brain SQLite database (application_id={application_id})"
                )
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported Brain SQLite schema {version}; expected {SCHEMA_VERSION}"
                )
            row = self.connection.execute(
                "SELECT schema_version, build_state, snapshot_id, base_snapshot_id, "
                "projection_id "
                "FROM snapshot WHERE singleton = 1"
            ).fetchone()
            if row is None or row[1] != "complete":
                raise StoreError("Brain SQLite snapshot is incomplete")
            if row[0] != version:
                raise StoreError("Brain SQLite schema version markers disagree")
            if row[2] != row[3]:
                raise StoreError("Brain SQLite snapshot identity aliases disagree")
            self.snapshot_id = row[2]
            self.base_snapshot_id = row[3]
            self.projection_id = row[4]
            paths = _paths(data_dir, artifact_paths)
            artifact_rows = list(
                self.connection.execute(
                    "SELECT name, digest, source_digest, logical_digest, raw_digest, "
                    "source_present, metadata_json FROM artifacts"
                )
            )
            recorded: dict[str, str | None] = {}
            logical_digests: dict[str, str] = {}
            stored_metadata: dict[str, dict[str, Any]] = {}
            source_presence: dict[str, bool] = {}
            for (
                name,
                digest,
                source_digest,
                logical_digest,
                raw_digest,
                source_present,
                metadata_json,
            ) in artifact_rows:
                if digest != logical_digest or source_digest != raw_digest:
                    raise StoreError(
                        f"Brain SQLite digest aliases disagree for {name}"
                    )
                recorded[name] = raw_digest
                logical_digests[name] = logical_digest
                source_presence[name] = bool(source_present)
                stored_metadata[name] = json.loads(metadata_json)
            present = {"nodes", "edges", "edges_links"}
            if paths["cells"].exists() or paths["synapses"].exists():
                if not paths["cells"].exists() or not paths["synapses"].exists():
                    raise StaleSnapshotError("Brain cell JSONL generation is incomplete")
                present.update(("cells", "synapses"))
            recorded_names = set(recorded)
            base_required = {"nodes", "edges", "edges_links"}
            if not base_required <= recorded_names or recorded_names != present:
                raise StaleSnapshotError(
                    "Brain SQLite snapshot has a different artifact set; rebuild it"
                )
            if require_derived and not {"cells", "synapses"} <= recorded_names:
                raise StaleSnapshotError(
                    "Brain SQLite snapshot has no derived cell layer; rebuild it"
                )
            for name in sorted(recorded_names):
                path_present = paths[name].exists()
                if path_present != source_presence[name]:
                    raise StaleSnapshotError(
                        f"Brain SQLite source presence differs for {paths[name].name}; "
                        "rebuild it"
                    )
                current_digest = digest_file(paths[name]) if path_present else None
                if current_digest != recorded[name]:
                    raise StaleSnapshotError(
                        "Brain SQLite snapshot is stale relative to "
                        f"{paths[name].name}; rebuild it"
                    )
            identity_artifacts = {
                name: (stored_metadata[name], []) for name in recorded_names
            }
            validate_generations(identity_artifacts)
            expected_base = base_snapshot_id_for(
                identity_artifacts, logical_digests
            )
            expected_projection = projection_id_for(
                identity_artifacts, logical_digests
            )
            if self.base_snapshot_id != expected_base:
                raise StoreError("Brain SQLite base snapshot identity is invalid")
            if self.projection_id != expected_projection:
                raise StoreError("Brain SQLite projection identity is invalid")
        except StoreError:
            if self.connection is not None:
                self.connection.close()
            raise
        except (json.JSONDecodeError, sqlite3.Error) as exc:
            if self.connection is not None:
                self.connection.close()
            raise StoreError(f"invalid Brain SQLite snapshot: {exc}") from exc

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()

    def metadata(self, artifact: str | None = None) -> dict[str, Any]:
        if artifact is not None:
            row = self.connection.execute(
                "SELECT metadata_json FROM artifacts WHERE name = ?", (artifact,)
            ).fetchone()
            return json.loads(row[0]) if row else {}
        return {
            name: json.loads(meta)
            for name, meta in self.connection.execute(
                "SELECT name, metadata_json FROM artifacts ORDER BY name"
            )
        }

    get_metadata = metadata

    def iter_nodes(self):
        for (payload,) in self.connection.execute(
            "SELECT payload_json FROM nodes ORDER BY ordinal"
        ):
            yield json.loads(payload)

    @staticmethod
    def _edge_query(endpoint=None, direction="both", kinds=None, stream=None):
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        values = sorted(kinds) if kinds else []

        if endpoint is not None:
            def branch(
                endpoint_column: str,
                index_name: str,
                *,
                exclude_self_loop: bool = False,
            ) -> tuple[str, list[Any]]:
                clauses = [f"{endpoint_column} = ?"]
                params: list[Any] = [endpoint]
                if exclude_self_loop:
                    clauses.append("src <> ?")
                    params.append(endpoint)
                if stream is not None:
                    clauses.append("stream = ?")
                    params.append(stream)
                if values:
                    clauses.append(
                        f"kind IN ({','.join('?' for _ in values)})"
                    )
                    params.extend(values)
                return (
                    "SELECT payload_json, stream, ordinal "
                    f"FROM edges INDEXED BY {index_name} WHERE "
                    + " AND ".join(clauses),
                    params,
                )

            if direction == "out":
                branches = [branch("src", "edges_src_kind_idx")]
            elif direction == "in":
                branches = [branch("dst", "edges_dst_kind_idx")]
            else:
                # The incoming branch excludes self-loops already returned by src.
                branches = [
                    branch("src", "edges_src_kind_idx"),
                    branch(
                        "dst",
                        "edges_dst_kind_idx",
                        exclude_self_loop=True,
                    ),
                ]
            sql = (
                "SELECT payload_json FROM ("
                + " UNION ALL ".join(part[0] for part in branches)
                + ") ORDER BY CASE stream WHEN 'main' THEN 0 ELSE 1 END, ordinal"
            )
            return sql, [param for part in branches for param in part[1]]

        clauses, params = [], []
        if stream is not None:
            clauses.append("stream = ?")
            params.append(stream)
        if values:
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            params.extend(values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT payload_json FROM edges"
            + where
            + " ORDER BY CASE stream WHEN 'main' THEN 0 ELSE 1 END, ordinal"
        )
        return sql, params

    def iter_edges(self, endpoint=None, direction="both", kinds=None, stream=None):
        sql, params = self._edge_query(endpoint, direction, kinds, stream)
        for (payload,) in self.connection.execute(sql, params):
            yield json.loads(payload)

    def edge_query_plan(
        self, endpoint=None, direction="both", kinds=None, stream=None
    ) -> tuple[str, ...]:
        """Return deterministic SQLite plan details for diagnostics and canaries."""
        sql, params = self._edge_query(endpoint, direction, kinds, stream)
        return tuple(
            row[3]
            for row in self.connection.execute("EXPLAIN QUERY PLAN " + sql, params)
        )

    def iter_cells(self):
        for (payload,) in self.connection.execute(
            "SELECT payload_json FROM cells ORDER BY ordinal"
        ):
            yield json.loads(payload)

    def resolve_owner(self, key: str) -> str | None:
        if key.startswith(("cell:", "path:")):
            return key
        row = self.connection.execute(
            "SELECT owner_id FROM organ_owners WHERE organ_id = ?", (key,)
        ).fetchone()
        if row:
            return row[0]
        bare = (
            key.split(":", 2)[2]
            if key.startswith("decl:") and key.count(":") >= 2
            else key
        )
        row = self.connection.execute(
            "SELECT owner_id FROM organ_owners WHERE bare_decl = ? ORDER BY organ_id LIMIT 1",
            (bare,),
        ).fetchone()
        if row:
            return row[0]
        row = self.connection.execute(
            "SELECT id FROM cells WHERE label = ? ORDER BY ordinal LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row else None

    def iter_synapses(self, endpoint=None, kinds=None):
        if endpoint is None:
            cursor = self.connection.execute(
                "SELECT payload_json FROM synapses ORDER BY ordinal"
            )
        else:
            cursor = self.connection.execute(
                "SELECT payload_json FROM synapses WHERE src = ? OR dst = ? ORDER BY ordinal",
                (endpoint, endpoint),
            )
        for (payload,) in cursor:
            row = json.loads(payload)
            if kinds and not (set(row.get("kinds") or {}) & set(kinds)):
                continue
            if kinds:
                row = {
                    **row,
                    "traces": [
                        t for t in row.get("traces", []) if t.get("kind") in kinds
                    ],
                }
            yield row


def open_store(
    *,
    data_dir: str | os.PathLike[str],
    backend: str | None = None,
    sqlite_path: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
    artifact_paths: Mapping[str, str | os.PathLike[str]] | None = None,
    require_derived: bool = False,
):
    """Open a validated read backend.

    ``auto`` uses SQLite only when every adjacent JSONL digest matches. Otherwise
    it falls back to a self-consistent JSONL generation.
    """
    selected = backend or os.environ.get("WIKILEAN_BRAIN_BACKEND", "auto")
    if selected not in {"auto", "sqlite", "jsonl"}:
        raise ValueError("backend must be auto, sqlite, or jsonl")
    root = Path(data_dir)
    database = Path(db_path or sqlite_path or root / DEFAULT_SQLITE_NAME)
    if selected == "jsonl":
        return JsonlStore(root, artifact_paths)
    if selected == "sqlite":
        return SQLiteStore(
            database, root, artifact_paths, require_derived=require_derived
        )
    try:
        return SQLiteStore(
            database, root, artifact_paths, require_derived=require_derived
        )
    except StoreError:
        return JsonlStore(root, artifact_paths)
