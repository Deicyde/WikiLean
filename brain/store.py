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
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
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


def _iter_handle(handle, path: Path) -> Iterator[dict[str, Any]]:
    handle.seek(0)
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
        digest = hashlib.sha256()
        digest.update(canonical_json(dict(meta)).encode("utf-8"))
        digest.update(b"\n")
        digest.update(digest_rows(rows).encode("ascii"))
        out[name] = digest.hexdigest()
    return out


def snapshot_id_for(
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
) -> str:
    digest = hashlib.sha256()
    for name in ("nodes", "edges", "edges_links"):
        meta, rows = artifacts[name]
        identity_meta = {k: v for k, v in meta.items() if k != "snapshot_id"}
        digest.update(name.encode("ascii"))
        digest.update(canonical_json(identity_meta).encode("utf-8"))
        digest.update(digest_rows(rows).encode("ascii"))
    return digest.hexdigest()


def _derive_owners(
    cells: Iterable[dict[str, Any]], cell_meta: Mapping[str, Any]
) -> dict[str, tuple[str, str | None, str | None]]:
    owners: dict[str, tuple[str, str | None, str | None]] = {}

    def claim(organ: Mapping[str, Any], owner: str) -> None:
        organ_id = organ.get("id")
        if not isinstance(organ_id, str):
            return
        kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
        bare = (
            organ_id.split(":", 2)[2]
            if kind == "decl" and organ_id.count(":") >= 2
            else None
        )
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
  metadata_json TEXT NOT NULL
);
CREATE TABLE artifacts (
  name TEXT PRIMARY KEY,
  generated_at TEXT,
  row_count INTEGER NOT NULL,
  digest TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  metadata_json TEXT NOT NULL
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


def write_sqlite_snapshot(
    path: str | os.PathLike[str],
    artifacts: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
    *,
    source_digests: Mapping[str, str] | None = None,
) -> Path:
    """Build and atomically publish a complete SQLite projection."""
    validate_generations(artifacts)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot_id = snapshot_id_for(artifacts)
    digests = artifact_digests(artifacts)
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
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        base_meta = dict(artifacts["nodes"][0])
        connection.execute(
            "INSERT INTO snapshot VALUES (1, ?, 'building', ?, ?)",
            (SCHEMA_VERSION, snapshot_id, canonical_json(base_meta)),
        )
        for name, (meta, rows) in artifacts.items():
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name,
                    meta.get("generated_at"),
                    len(rows),
                    digests[name],
                    (source_digests or {}).get(name, digests[name]),
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
        connection.execute(
            "UPDATE snapshot SET build_state = 'complete' WHERE singleton = 1"
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StoreError("SQLite integrity_check failed")
        connection.close()
        connection = None
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return target
    except (KeyError, sqlite3.Error) as exc:
        raise StoreError(f"could not build SQLite snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)


def write_sqlite_from_jsonl(path: Path, data_dir: Path) -> Path:
    """Stream a consistent JSONL generation into an atomic SQLite index."""
    paths = _paths(data_dir)
    metadata = {
        "nodes": read_metadata(paths["nodes"]),
        "edges": read_metadata(paths["edges"]),
        "edges_links": read_metadata(paths["edges_links"], optional=True),
    }
    if paths["cells"].exists() or paths["synapses"].exists():
        metadata["cells"] = read_metadata(paths["cells"])
        metadata["synapses"] = read_metadata(paths["synapses"])
    validate_generations({name: (meta, []) for name, meta in metadata.items()})

    source_digests = {
        name: digest_file(paths[name]) if paths[name].exists()
        else artifact_digests({name: ({}, [])})[name]
        for name in metadata
    }
    identity = hashlib.sha256()
    for name in ("nodes", "edges", "edges_links"):
        identity.update(name.encode("ascii"))
        identity.update(source_digests[name].encode("ascii"))
    snapshot_id = identity.hexdigest()

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    connection: sqlite3.Connection | None = None
    counts: dict[str, int] = {}
    try:
        connection = sqlite3.connect(temp)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO snapshot VALUES (1, ?, 'building', ?, ?)",
            (SCHEMA_VERSION, snapshot_id, canonical_json(metadata["nodes"])),
        )

        def insert_nodes() -> Iterator[tuple[Any, ...]]:
            count = 0
            for count, row in enumerate(iter_jsonl(paths["nodes"]), 1):
                yield (count - 1, row["id"], row["type"], row.get("label"), canonical_json(row))
            counts["nodes"] = count

        connection.executemany("INSERT INTO nodes VALUES (?, ?, ?, ?, ?)", insert_nodes())

        for stream, name in (("main", "edges"), ("links", "edges_links")):
            def insert_edges(stream=stream, name=name) -> Iterator[tuple[Any, ...]]:
                count = 0
                for count, row in enumerate(
                    iter_jsonl(paths[name], optional=name == "edges_links"), 1
                ):
                    provenance = row.get("provenance") or {}
                    yield (
                        stream, count - 1, row["src"], row["dst"], row["kind"],
                        row.get("confidence"), provenance.get("source"), canonical_json(row),
                    )
                counts[name] = count

            connection.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)", insert_edges()
            )

        if "cells" in metadata:
            def insert_cells() -> Iterator[tuple[Any, ...]]:
                count = 0
                for count, row in enumerate(iter_jsonl(paths["cells"]), 1):
                    yield (count - 1, row["id"], row.get("anchor"), row.get("label"), canonical_json(row))
                counts["cells"] = count

            connection.executemany("INSERT INTO cells VALUES (?, ?, ?, ?, ?)", insert_cells())

            # Cell ownership wins over a supercell listing for the same concept.
            owner_count = 0
            for cell in iter_jsonl(paths["cells"]):
                for organ in cell.get("organs") or []:
                    organ_id = organ.get("id")
                    if not isinstance(organ_id, str):
                        continue
                    kind = organ.get("kind") if isinstance(organ.get("kind"), str) else None
                    bare = organ_id.split(":", 2)[2] if kind == "decl" and organ_id.count(":") >= 2 else None
                    connection.execute(
                        "INSERT INTO organ_owners VALUES (?, ?, ?, ?)",
                        (organ_id, cell["id"], kind, bare),
                    )
                    owner_count += 1
            for owner, organs in (metadata["cells"].get("supercell_organs") or {}).items():
                for organ in organs:
                    organ_id = organ.get("id")
                    if not isinstance(organ_id, str):
                        continue
                    before = connection.total_changes
                    connection.execute(
                        "INSERT OR IGNORE INTO organ_owners VALUES (?, ?, ?, NULL)",
                        (organ_id, owner, organ.get("kind")),
                    )
                    owner_count += connection.total_changes - before
            counts["owners"] = owner_count

            def insert_synapses() -> Iterator[tuple[Any, ...]]:
                count = 0
                for count, row in enumerate(iter_jsonl(paths["synapses"]), 1):
                    yield (count - 1, row["src"], row["dst"], row["weight"], canonical_json(row))
                counts["synapses"] = count

            connection.executemany(
                "INSERT INTO synapses VALUES (?, ?, ?, ?, ?)", insert_synapses()
            )

        for name, meta in metadata.items():
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name, meta.get("generated_at"), counts.get(name, 0),
                    source_digests[name], source_digests[name], canonical_json(meta),
                ),
            )
        connection.execute("UPDATE snapshot SET build_state = 'complete' WHERE singleton = 1")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StoreError("SQLite integrity_check failed")
        connection.close()
        connection = None
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return target
    except (KeyError, sqlite3.Error) as exc:
        raise StoreError(f"could not import JSONL snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        temp.unlink(missing_ok=True)


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
        uri = f"file:{path.resolve()}?mode=ro"
        self.connection: sqlite3.Connection | None = None
        try:
            self.connection = sqlite3.connect(uri, uri=True)
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported Brain SQLite schema {version}; expected {SCHEMA_VERSION}"
                )
            row = self.connection.execute(
                "SELECT build_state, snapshot_id FROM snapshot WHERE singleton = 1"
            ).fetchone()
            if row is None or row[0] != "complete":
                raise StoreError("Brain SQLite snapshot is incomplete")
            self.snapshot_id = row[1]
            paths = _paths(data_dir, artifact_paths)
            recorded = dict(
                self.connection.execute("SELECT name, source_digest FROM artifacts")
            )
            present = {"nodes", "edges", "edges_links"}
            if paths["cells"].exists() or paths["synapses"].exists():
                if not paths["cells"].exists() or not paths["synapses"].exists():
                    raise StaleSnapshotError("Brain cell JSONL generation is incomplete")
                present.update(("cells", "synapses"))
            recorded_names = set(recorded)
            base_required = {"nodes", "edges", "edges_links"}
            if not base_required <= recorded_names or recorded_names - present:
                raise StaleSnapshotError(
                    "Brain SQLite snapshot has a different artifact set; rebuild it"
                )
            if require_derived and not {"cells", "synapses"} <= recorded_names:
                raise StaleSnapshotError(
                    "Brain SQLite snapshot has no derived cell layer; rebuild it"
                )
            for name in sorted(recorded_names):
                current_digest = (
                    digest_file(paths[name])
                    if paths[name].exists()
                    else artifact_digests({name: ({}, [])})[name]
                )
                if current_digest != recorded[name]:
                    raise StaleSnapshotError(
                        f"Brain SQLite snapshot is stale relative to {paths[name].name}; rebuild it"
                    )
        except StoreError:
            if self.connection is not None:
                self.connection.close()
            raise
        except sqlite3.Error as exc:
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

    def iter_edges(self, endpoint=None, direction="both", kinds=None, stream=None):
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        clauses, params = [], []
        if stream is not None:
            clauses.append("stream = ?")
            params.append(stream)
        if endpoint is not None:
            if direction == "out":
                clauses.append("src = ?")
                params.append(endpoint)
            elif direction == "in":
                clauses.append("dst = ?")
                params.append(endpoint)
            else:
                clauses.append("(src = ? OR dst = ?)")
                params.extend((endpoint, endpoint))
        if kinds:
            values = sorted(kinds)
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            params.extend(values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT payload_json FROM edges"
            + where
            + " ORDER BY CASE stream WHEN 'main' THEN 0 ELSE 1 END, ordinal"
        )
        for (payload,) in self.connection.execute(sql, params):
            yield json.loads(payload)

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
