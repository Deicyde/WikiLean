#!/usr/bin/env python3
"""Build the generated SQLite BRAIN snapshot.

Library callers use :func:`build_snapshot` to index an existing consistent JSONL
snapshot without rewriting it. The CLI's default mode performs the canonical
organ build once, publishes nodes plus both edge streams, then indexes those
exact bytes. Use ``--jsonl-only`` to publish only the three base-graph JSONL
artifacts, or ``--from-jsonl`` to rebuild only the local SQLite index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from build_common import BRAIN_DATA, build, write_edges, write_jsonl
from store import DEFAULT_SQLITE_NAME, digest_file, write_sqlite_from_jsonl


def build_snapshot(*, data_dir: Path, output: Path) -> Path:
    """Index existing JSONL artifacts into an atomic SQLite snapshot."""
    return write_sqlite_from_jsonl(Path(output), Path(data_dir))


def _publish_live_graph(*, data_dir: Path, sqlite_output: Path | None) -> str:
    """Build and atomically publish the base graph, optionally with SQLite."""
    data_dir = Path(data_dir)
    sqlite_output = Path(sqlite_output) if sqlite_output is not None else None
    nodes, edges, meta = build()

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".brain-snapshot.", dir=data_dir.parent))
    try:
        staged_nodes = staging / "nodes.jsonl"
        staged_edges = staging / "edges.jsonl"
        staged_links = staging / "edges_links.jsonl"
        write_jsonl(staged_nodes, meta, nodes)
        write_edges(edges, meta, staged_edges, staged_links)

        # generated_at is the version pin for legacy readers. A separate stable
        # snapshot ID lets new readers detect a mixed multi-file publication.
        identity = hashlib.sha256()
        for artifact in (staged_nodes, staged_edges, staged_links):
            identity.update(digest_file(artifact).encode("ascii"))
        snapshot_id = identity.hexdigest()
        meta_with_id = {**meta, "snapshot_id": snapshot_id}
        write_jsonl(staged_nodes, meta_with_id, nodes)
        write_edges(edges, meta_with_id, staged_edges, staged_links)

        publications: list[tuple[Path, Path]] = [
            (staged_nodes, data_dir / "nodes.jsonl"),
            (staged_edges, data_dir / "edges.jsonl"),
            (staged_links, data_dir / "edges_links.jsonl"),
        ]
        if sqlite_output is not None:
            staged_db = staging / DEFAULT_SQLITE_NAME
            write_sqlite_from_jsonl(staged_db, staging)
            publications.append((staged_db, sqlite_output))
        data_dir.mkdir(parents=True, exist_ok=True)
        backups: list[tuple[Path, Path | None]] = []
        try:
            for index, (source, destination) in enumerate(publications):
                backup = staging / f"previous-{index}"
                if destination.exists():
                    try:
                        os.link(destination, backup)
                    except OSError:
                        shutil.copy2(destination, backup)
                    backups.append((destination, backup))
                else:
                    backups.append((destination, None))
                os.replace(source, destination)
        except BaseException:
            for destination, backup in reversed(backups):
                if backup is None:
                    destination.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, destination)
            raise
        return snapshot_id
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_jsonl_only(*, data_dir: Path = BRAIN_DATA) -> str:
    """Publish nodes and both edge streams without creating or replacing SQLite."""
    return _publish_live_graph(data_dir=Path(data_dir), sqlite_output=None)


def build_live_snapshot(*, data_dir: Path = BRAIN_DATA, output: Path | None = None) -> Path:
    """Build the organ graph once and publish JSONL plus its SQLite projection."""
    data_dir = Path(data_dir)
    output = Path(output or data_dir / DEFAULT_SQLITE_NAME)
    _publish_live_graph(data_dir=data_dir, sqlite_output=output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BRAIN_DATA)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--from-jsonl",
        action="store_true",
        help="index existing JSONL without rebuilding or rewriting tracked artifacts",
    )
    mode.add_argument(
        "--jsonl-only",
        action="store_true",
        help="build and atomically publish nodes/edges JSONL without touching SQLite",
    )
    args = parser.parse_args()
    if args.jsonl_only:
        snapshot_id = build_jsonl_only(data_dir=args.data_dir)
        print(json.dumps({
            "data_dir": str(args.data_dir),
            "snapshot_id": snapshot_id,
        }, sort_keys=True))
        return 0
    output = args.output or args.data_dir / DEFAULT_SQLITE_NAME
    if args.from_jsonl:
        result = build_snapshot(data_dir=args.data_dir, output=output)
    else:
        result = build_live_snapshot(data_dir=args.data_dir, output=output)
    size_mb = result.stat().st_size / 1024 / 1024
    print(json.dumps({"sqlite": str(result), "size_mb": round(size_mb, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
