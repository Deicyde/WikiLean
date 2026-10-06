#!/usr/bin/env python3
"""Import atomic companion shards into an isolated, local Brain SQLite projection.

The reference FCA package owns validation and discovery. This adapter owns only
transport provenance and a conservative association projection: raw URI endpoints,
no invented declarations/concepts, and no formalization or review claims.
"""
from __future__ import annotations

import argparse
import builtins
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brain import store

REFERENCE_VERSION = "2.0.0a2"
PROFILE = "companion-local-import-v1"
REFERENCE_PARTS = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):)?"
    r"(?://(?P<authority>[^/?#]*))?(?P<path>[^?#]*)"
    r"(?:\?(?P<query>[^#]*))?(?:#(?P<fragment>.*))?$"
)
SOURCE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ImportError(ValueError):
    """An input batch or destination cannot be imported safely."""


def _collect(paths, *, include, exclude):
    try:
        import formal_companion_annotations as fca
        if getattr(fca, "__version__", None) != REFERENCE_VERSION:
            raise ImportError(
                f"FCA {REFERENCE_VERSION} is required for this draft integration; "
                f"found {getattr(fca, '__version__', 'unknown')}. "
                "See docs/COMPANION-IMPORT.md."
            )
        from formal_companion_annotations.discovery import collect
    except builtins.ImportError as exc:
        raise ImportError(
            "Install the FCA 2.0.0a2 reference package in this Python environment; "
            "see docs/COMPANION-IMPORT.md. Validation is required, never bypassed."
        ) from exc
    return collect(paths, include=include, exclude=exclude)


def _json(value: Any) -> str:
    # Keep insertion order in the recovery string even though the Brain store's
    # canonical payload encoding sorts object keys. Arrays and large ints survive.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _remove_local_dot_segments(path: str) -> str:
    """Normalize literal dot segments of an absolute carrier path, preserving //."""
    if not path:
        return path
    if not path.startswith("/"):
        raise ValueError("carrier resolution requires an absolute hierarchical base path")
    output = [""]
    parts = path.split("/")[1:]
    for index, part in enumerate(parts):
        if part == "..":
            if len(output) > 1:
                output.pop()
        elif part != ".":
            output.append(part)
        if part in (".", "..") and index == len(parts) - 1:
            output.append("")
    return "/".join(output)


def _resolve_reference(base_uri: str, reference: str) -> tuple[str, dict[str, str]]:
    """Produce a lookup key without silently rewriting opaque identifiers.

    Relative references use RFC 3986 section 5.2 component/path merging. The
    carrier is always an absolute file URI. Core validation is only lexical;
    unsupported authority syntax is retained with explicit unresolved status.
    """
    parsed = REFERENCE_PARTS.fullmatch(reference)
    assert parsed is not None  # FCA's lexical rule excludes control characters.
    parts = parsed.groupdict()
    try:
        # Inspect only; never use urllib's recomposition, case folding, percent
        # decoding, or legacy same-scheme relative handling for output keys.
        urlsplit(reference)
    except ValueError:
        if parts["scheme"] is not None:
            key = reference
        else:
            digest = hashlib.sha256(_json([base_uri, reference]).encode()).hexdigest()
            key = "urn:wikilean:companion:unresolved:" + digest
        return key, {"status": "unresolved", "reason": "unsupported_authority_syntax"}
    if parts["scheme"] is not None:
        return reference, {"status": "absolute"}
    base = REFERENCE_PARTS.fullmatch(base_uri).groupdict()
    authority = parts["authority"]
    path = parts["path"]
    query = parts["query"]
    if authority is not None:
        path = _remove_local_dot_segments(path)
    else:
        authority = base["authority"]
        if not path:
            path = base["path"]
            if query is None:
                query = base["query"]
        else:
            if not path.startswith("/"):
                path = base["path"].rsplit("/", 1)[0] + "/" + path
            path = _remove_local_dot_segments(path)
    key = base["scheme"] + ":"
    if authority is not None:
        key += "//" + authority
    key += path
    if query is not None:
        key += "?" + query
    if parts["fragment"] is not None:
        key += "#" + parts["fragment"]
    return key, {"status": "resolved"}


def _projection(collection, source_key: str):
    if collection.problems:
        findings = [f"{p.code} {p.path}: {p.message}" for p in collection.problems]
        raise ImportError("Invalid companion batch; nothing imported:\n" + "\n".join(findings))
    if not collection.records:
        raise ImportError("No annotations selected; nothing imported")
    edges = []
    for record in collection.records:
        annotation = record.annotation
        origin = {"base_uri": record.base_uri, "pointer": record.pointer}
        if record.line is not None:
            origin["line"] = record.line
        encoded = _json(annotation)
        # Origin+ordinal identifies an occurrence, not mathematical identity.
        # Repeated IDs and even repeated content remain separate edge rows.
        identity = hashlib.sha256(_json([source_key, origin, encoded]).encode()).hexdigest()
        source, source_resolution = _resolve_reference(record.base_uri, annotation["source"]["uri"])
        target, target_resolution = _resolve_reference(record.base_uri, annotation["target"])
        edges.append({
            "src": source,
            "dst": target,
            "kind": "links",
            "confidence": "unreviewed",
            "provenance": {"source": source_key, "method": PROFILE},
            "evidence": {
                "occurrence": identity,
                "annotation": annotation,
                "annotation_json": encoded,
                "origin": origin,
                "resolution": {"source": source_resolution, "target": target_resolution},
            },
        })
    digest = hashlib.sha256(store.canonical_json([source_key, edges]).encode()).hexdigest()
    metadata = {
        "generated_at": f"sha256:{digest}",
        "profile": PROFILE,
        "source_key": source_key,
        "annotation_count": len(edges),
        "publication_status": "local-unreviewed",
        "note": "Source key is a local label, not a reviewed source-registry identity.",
    }
    for edge in edges:
        edge["provenance"]["pin"] = f"sha256:{digest}"
    return metadata, edges


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Publish atomically without replacing even a concurrently created empty directory."""
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    try:
        if sys.platform.startswith("linux"):
            rename = library.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(-100, source_bytes, -100, destination_bytes, 1)  # RENAME_NOREPLACE
        elif sys.platform == "darwin":
            rename = library.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
        else:
            raise AttributeError(sys.platform)
    except AttributeError as exc:
        raise ImportError(f"Atomic no-replace publication is unavailable on {sys.platform}") from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def import_companions(paths: Iterable[Path | str], *, source_key: str,
                      output: Path | str, include: Iterable[str] | None = None,
                      exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Validate the complete batch, then atomically publish a new local directory.

    The output must not exist. It contains the normal Brain JSONL artifacts and
    generated SQLite index. Resource URIs are indexed but never fetched or read.
    """
    if not SOURCE_KEY.fullmatch(source_key):
        raise ImportError("source-key must be 1–128 letters/digits/dots/underscores/hyphens, starting with a letter or digit")
    target = Path(output).absolute()
    if target.exists() or target.is_symlink():
        raise ImportError(f"Output already exists: {target}")
    collection = _collect(paths, include=include, exclude=exclude)
    metadata, edges = _projection(collection, source_key)
    # Failures before this point cannot create any output or parent directory.
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for name, rows in (("nodes", []), ("edges", []), ("edges_links", edges)):
            with (stage / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(_json({"_meta": metadata}) + "\n")
                for row in rows:
                    handle.write(_json(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        store.write_sqlite_from_jsonl(stage / store.DEFAULT_SQLITE_NAME, stage)
        # Exercise the production freshness/integrity reader before publication.
        database = store.open_store(data_dir=stage, backend="sqlite")
        database.close()
        if target.exists() or target.is_symlink():
            raise ImportError(f"Output appeared during import: {target}")
        _rename_no_replace(stage, target)
        return {"output": str(target), "database": str(target / store.DEFAULT_SQLITE_NAME),
                "annotations": len(edges), "files": len(collection.files),
                "generation": metadata["generated_at"], "profile": PROFILE}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def iter_annotations(data_dir: Path | str, *, source: str | None = None,
                     target: str | None = None):
    """Recover atomic records and origins through the existing indexed reader."""
    database = store.open_store(data_dir=data_dir, backend="sqlite")
    try:
        if database.metadata("nodes").get("profile") != PROFILE:
            raise ImportError("Not a local companion import projection")
        endpoint = source if source is not None else target
        direction = "out" if source is not None else "in"
        for edge in database.iter_edges(endpoint=endpoint, direction=direction,
                                        kinds={"links"}, stream="links"):
            if target is not None and edge["dst"] != target:
                continue
            evidence = edge["evidence"]
            yield {"annotation": json.loads(evidence["annotation_json"]),
                   "origin": evidence["origin"],
                   "source": edge["src"], "target": edge["dst"],
                   "resolution": evidence.get("resolution", {
                       "source": {"status": "legacy"}, "target": {"status": "legacy"}})}
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="validate shards and build a new local Brain index")
    ingest.add_argument("paths", nargs="+")
    ingest.add_argument("--source-key", required=True)
    ingest.add_argument("--output", required=True, type=Path)
    ingest.add_argument("--include", action="append")
    ingest.add_argument("--exclude", action="append", default=[])
    query = commands.add_parser("query", help="read original annotations and origins from SQLite")
    query.add_argument("--data-dir", required=True, type=Path)
    query.add_argument("--source", help="exact source lookup key from query output")
    query.add_argument("--target", help="exact target lookup key from query output")
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            print(_json(import_companions(args.paths, source_key=args.source_key,
                                         output=args.output, include=args.include,
                                         exclude=args.exclude)))
        else:
            for result in iter_annotations(args.data_dir, source=args.source, target=args.target):
                print(_json(result))
        return 0
    except (ImportError, store.StoreError, OSError, ValueError) as exc:
        print(f"companion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
