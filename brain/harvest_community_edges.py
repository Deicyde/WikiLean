#!/usr/bin/env python3
"""Graduate community edges from one verified, sealed D1 snapshot bundle.

The harvester is deliberately not an acquisition client. It accepts only an
explicit bundle produced by ``brain/acquire_d1_snapshot.py`` and delegates the
complete bundle check to the shared consumer-side verifier.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import authority_contracts as contracts  # noqa: E402
import stage_io  # noqa: E402
from d1_snapshot_bundle import (  # noqa: E402
    ACTOR_TYPES,
    COMMUNITY_KINDS,
    ROW_STATUSES,
    SnapshotBundle,
    SnapshotBundleError,
    _canonical_line,
    verify_snapshot_bundle,
)

HarvestError = SnapshotBundleError

NODES = BRAIN / "data" / "nodes.jsonl"
OUT = BRAIN / "data" / "community_edges.jsonl"
XREF_DBS = {
    "mathworld", "nlab", "proofwiki", "eom", "planetmath", "metamath",
    "lmfdb_knowl", "oeis", "dlmf", "msc", "stacks", "kerodon", "kgmid",
}


def load_node_ids(path: Path = NODES) -> set[str]:
    """Load a generated node file with exactly one first-line metadata row."""
    ids: set[str] = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HarvestError(f"cannot read static Brain nodes {path}: {exc}") from exc
    if not lines:
        raise HarvestError(f"{path}: empty node file")
    for index, line in enumerate(lines, start=1):
        if not line:
            raise HarvestError(f"{path}:{index}: blank lines are forbidden")
        try:
            row = contracts.parse_artifact_json_bytes(
                line.encode("utf-8"), location=f"{path}:{index}"
            )
        except contracts.VerificationError as exc:
            raise HarvestError(str(exc)) from exc
        if index == 1:
            if (
                not isinstance(row, dict)
                or set(row) != {"_meta"}
                or not isinstance(row["_meta"], dict)
            ):
                raise HarvestError(f"{path}: first line must be exactly one _meta object")
            continue
        if not isinstance(row, dict):
            raise HarvestError(f"{path}:{index}: expected node object")
        if "_meta" in row:
            raise HarvestError(f"{path}:{index}: metadata is permitted only on the first line")
        node_id = row.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise HarvestError(f"{path}:{index}: node lacks a non-empty id")
        if node_id in ids:
            raise HarvestError(f"{path}:{index}: duplicate node id {node_id!r}")
        ids.add(node_id)
    return ids


def validate_edge(
    row: Mapping[str, Any], node_ids: set[str], pin: str
) -> tuple[dict[str, Any] | None, str]:
    """Graduate against only the sealed static-plus-community node universe."""
    if not contracts.HASH_RE.fullmatch(pin):
        raise HarvestError("community provenance pin must be an authority identity")
    actor = row.get("actor_type")
    status = row.get("status")
    kind = row.get("kind")
    if actor not in ACTOR_TYPES:
        raise HarvestError(f"unknown actor {actor!r}")
    if status not in ROW_STATUSES:
        raise HarvestError(f"unknown status {status!r}")
    if kind not in COMMUNITY_KINDS:
        raise HarvestError(f"unknown community kind {kind!r}")
    if status != "live":
        return None, "deleted"
    src, dst = row.get("src"), row.get("dst")
    if src not in node_ids:
        return None, "src not a known node"
    if kind == "xref":
        if not (isinstance(dst, str) and dst.startswith("xref:")):
            return None, "xref dst malformed"
        parts = dst.split(":")
        if len(parts) < 3 or parts[1] not in XREF_DBS or not parts[2]:
            return None, "unknown/empty xref db"
    elif dst not in node_ids:
        return None, "dst not a known node"
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        raise HarvestError("edge evidence must be a normalized object")
    edge = {
        "src": src,
        "dst": dst,
        "kind": kind,
        "provenance": {
            "source": "community",
            "method": f"community-{actor} (brain_edges)",
            "pin": pin,
        },
        "confidence": "high" if actor == "human" else "medium",
        "evidence": {
            **evidence,
            "added_by": row.get("added_by"),
            "actor_type": actor,
            "edge_id": row.get("id"),
        },
    }
    return edge, ""


def harvest(
    rows: Sequence[Mapping[str, Any]], node_ids: set[str], pin: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for row in rows:
        edge, reason = validate_edge(row, node_ids, pin)
        if edge is not None:
            kept.append(edge)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    kept.sort(key=contracts.canonical_artifact_json_bytes)
    return kept, dropped


def _output_bytes(edges: Sequence[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_line(edge) for edge in edges)


def write_output(path: Path, data: bytes) -> None:
    """Durably replace one output with fully staged bytes."""
    path = Path(path).absolute()
    parent = path.parent
    try:
        parent_meta = parent.lstat()
    except OSError as exc:
        raise HarvestError(f"cannot inspect output directory {parent}: {exc}") from exc
    if stat.S_ISLNK(parent_meta.st_mode) or not stat.S_ISDIR(parent_meta.st_mode):
        raise HarvestError(f"output parent is not a real directory: {parent}")
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise HarvestError(f"refusing to replace non-regular output: {path}")
    temporary = parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        stage_io.write_bytes_exclusive(temporary, data, mode=0o644)
        os.replace(temporary, path)
        stage_io.fsync_directory(parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(
    snapshot_bundle: Path,
    *,
    output: Path = OUT,
    static_nodes: Path = NODES,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int], SnapshotBundle]:
    bundle = verify_snapshot_bundle(snapshot_bundle)
    node_ids = load_node_ids(static_nodes)
    node_ids.update(row["id"] for row in bundle.nodes if row["status"] == "live")
    if not node_ids:
        raise HarvestError("node universe is empty")
    kept, dropped = harvest(bundle.edges, node_ids, bundle.normalization_lineage_id)
    if not dry_run:
        write_output(output, _output_bytes(kept))
    return kept, dropped, bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-bundle",
        type=Path,
        required=True,
        help="absolute sealed bundle directory produced by acquire_d1_snapshot.py",
    )
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--dry-run", action="store_true", help="verify and report without writing")
    args = parser.parse_args(argv)
    if not args.snapshot_bundle.is_absolute():
        parser.error("--snapshot-bundle must be an absolute path")
    try:
        kept, dropped, bundle = run(
            args.snapshot_bundle, output=args.output, dry_run=args.dry_run
        )
    except (HarvestError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    n_human = sum(edge["evidence"]["actor_type"] == "human" for edge in kept)
    n_ai = len(kept) - n_human
    print(
        f"community edges: {len(bundle.edges)} sealed rows -> {len(kept)} graduate "
        f"({n_human} human, {n_ai} AI-attributed); pin={bundle.normalization_lineage_id}"
    )
    for reason, count in sorted(dropped.items(), key=lambda item: (-item[1], item[0])):
        print(f"  dropped {count}: {reason}")
    print("(dry run - not written)" if args.dry_run else f"wrote {args.output} ({len(kept)} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
