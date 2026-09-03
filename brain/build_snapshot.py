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

import build_context
from build_common import (
    BRAIN_DATA,
    ContextBuildInputs,
    build,
    write_edges,
    write_jsonl,
)
from stage_io import (
    assert_outputs_absent,
    ensure_private_directory,
    owned_directory,
    publish_files_no_replace,
    require_same_filesystem,
)
from store import DEFAULT_SQLITE_NAME, digest_file, write_sqlite_from_jsonl


BASE_CONTEXT_STAGE = "base-graph"
BASE_CONTEXT_PROGRAM = "brain/build_snapshot.py"
BASE_CONTEXT_ARGV = ("--jsonl-only",)
BASE_CONTEXT_NEEDS: tuple[str, ...] = ()
BASE_CONTEXT_OUTPUTS = (
    "brain/data/edges.jsonl",
    "brain/data/edges_links.jsonl",
    "brain/data/nodes.jsonl",
)

SQLITE_CONTEXT_STAGE = "sqlite-with-cells"
SQLITE_CONTEXT_PROGRAM = "brain/build_snapshot.py"
SQLITE_CONTEXT_ARGV = ("--from-jsonl",)
SQLITE_CONTEXT_NEEDS = ("base-graph", "cells")
SQLITE_CONTEXT_OUTPUT = "brain/data/brain.sqlite3"
SQLITE_CONTEXT_INPUTS = (
    ("nodes", "base-graph", "brain/data/nodes.jsonl"),
    ("edges", "base-graph", "brain/data/edges.jsonl"),
    ("edges_links", "base-graph", "brain/data/edges_links.jsonl"),
    ("cells", "cells", "brain/data/cells.jsonl"),
    ("synapses", "cells", "brain/data/synapses.jsonl"),
)


def build_snapshot(*, data_dir: Path, output: Path) -> Path:
    """Index existing JSONL artifacts into an atomic SQLite snapshot."""
    return write_sqlite_from_jsonl(Path(output), Path(data_dir))


def build_base_graph_from_context(context: build_context.BuildContext) -> str:
    """Build and publish the three exact base-graph outputs from sealed inputs."""
    context.require_stage(
        BASE_CONTEXT_STAGE,
        program=BASE_CONTEXT_PROGRAM,
        argv=BASE_CONTEXT_ARGV,
        needs=BASE_CONTEXT_NEEDS,
        outputs=[("file", relative) for relative in BASE_CONTEXT_OUTPUTS],
    )
    outputs = {
        Path(relative).name: context.output_for(BASE_CONTEXT_STAGE, relative)
        for relative in BASE_CONTEXT_OUTPUTS
    }
    targets = tuple(outputs.values())
    assert_outputs_absent(targets)
    for parent in {target.parent for target in targets}:
        ensure_private_directory(context.roots.output, parent)

    scratch = context.scratch_for(BASE_CONTEXT_STAGE, "jsonl")
    with owned_directory(context.roots.scratch, scratch) as ownership:
        for parent in {target.parent for target in targets}:
            require_same_filesystem(scratch, parent)

        sources = ContextBuildInputs.from_context(
            context,
            materialize_root=scratch / "inputs",
        )
        nodes, edges, meta = build(source_set=sources)
        # Reverify both the private reducer copies and their bound sources after
        # final use, before any output bytes are written or published.
        sources.verify()
        sources.verify_sources()
        staged_nodes = scratch / "nodes.jsonl"
        staged_edges = scratch / "edges.jsonl"
        staged_links = scratch / "edges_links.jsonl"
        write_jsonl(staged_nodes, meta, nodes)
        write_edges(edges, meta, staged_edges, staged_links)

        # The legacy writers inherit umask. Normalize before re-opening the
        # staged files so even a hostile 0777 umask cannot make them unreadable.
        for artifact in (staged_nodes, staged_edges, staged_links):
            artifact.chmod(0o644)
        identity = hashlib.sha256()
        for artifact in (staged_nodes, staged_edges, staged_links):
            identity.update(digest_file(artifact).encode("ascii"))
        snapshot_id = identity.hexdigest()
        meta_with_id = {**meta, "snapshot_id": snapshot_id}
        write_jsonl(staged_nodes, meta_with_id, nodes)
        write_edges(edges, meta_with_id, staged_edges, staged_links)

        publications = (
            (staged_edges, outputs["edges.jsonl"]),
            (staged_links, outputs["edges_links.jsonl"]),
            (staged_nodes, outputs["nodes.jsonl"]),
        )
        for source, _target in publications:
            source.chmod(0o644)
        publish_files_no_replace(publications, scratch=ownership)
        return snapshot_id


def build_sqlite_from_context(context: build_context.BuildContext) -> Path:
    """Build the declared SQLite stage from its five direct-dependency files."""
    context.require_stage(
        SQLITE_CONTEXT_STAGE,
        program=SQLITE_CONTEXT_PROGRAM,
        argv=SQLITE_CONTEXT_ARGV,
        needs=SQLITE_CONTEXT_NEEDS,
        outputs=[("file", SQLITE_CONTEXT_OUTPUT)],
    )

    artifact_paths: dict[str, Path] = {}
    for artifact, owner, relative in SQLITE_CONTEXT_INPUTS:
        artifact_paths[artifact] = context.dependency_output_for(
            SQLITE_CONTEXT_STAGE, owner, relative
        )

    if len(set(artifact_paths.values())) != len(SQLITE_CONTEXT_INPUTS):
        raise build_context.BuildContextError(
            "SQLite stage inputs must be five distinct files"
        )

    output = context.output_for(SQLITE_CONTEXT_STAGE, SQLITE_CONTEXT_OUTPUT)
    if output in artifact_paths.values():
        raise build_context.BuildContextError("SQLite output overlaps an upstream artifact")
    scratch = context.scratch_for(SQLITE_CONTEXT_STAGE, "sqlite")
    assert_outputs_absent([output])
    ensure_private_directory(context.roots.output, output.parent)
    with owned_directory(context.roots.scratch, scratch) as ownership:
        require_same_filesystem(scratch, output.parent)

        def publish(temp: Path, target: Path) -> None:
            publish_files_no_replace([(temp, target)], scratch=ownership)

        return write_sqlite_from_jsonl(
            output,
            context.roots.output,
            artifact_paths=artifact_paths,
            required_artifacts=tuple(artifact_paths),
            temp_dir=scratch,
            publisher=publish,
        )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-context", type=Path)
    parser.add_argument("--stage-id")
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
    args = parser.parse_args(argv)
    if args.build_context is not None or args.stage_id is not None:
        if args.build_context is None or args.stage_id is None:
            parser.error("--build-context and --stage-id must be provided together")
        if args.data_dir is not None or args.output is not None:
            parser.error("context mode forbids --data-dir and --output")
        context = build_context.load_build_context(args.build_context)
        if args.stage_id == BASE_CONTEXT_STAGE:
            if not args.jsonl_only or args.from_jsonl:
                parser.error("base-graph context mode requires --jsonl-only")
            build_base_graph_from_context(context)
            return 0
        if args.stage_id == SQLITE_CONTEXT_STAGE:
            if not args.from_jsonl or args.jsonl_only:
                parser.error("sqlite-with-cells context mode requires --from-jsonl")
            build_sqlite_from_context(context)
            return 0
        parser.error(
            "this reducer supports context stages "
            f"{BASE_CONTEXT_STAGE!r} and {SQLITE_CONTEXT_STAGE!r}"
        )

    data_dir = args.data_dir or BRAIN_DATA
    if args.jsonl_only:
        snapshot_id = build_jsonl_only(data_dir=data_dir)
        print(json.dumps({
            "data_dir": str(data_dir),
            "snapshot_id": snapshot_id,
        }, sort_keys=True))
        return 0
    output = args.output or data_dir / DEFAULT_SQLITE_NAME
    if args.from_jsonl:
        result = build_snapshot(data_dir=data_dir, output=output)
    else:
        result = build_live_snapshot(data_dir=data_dir, output=output)
    size_mb = result.stat().st_size / 1024 / 1024
    print(json.dumps({"sqlite": str(result), "size_mb": round(size_mb, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
