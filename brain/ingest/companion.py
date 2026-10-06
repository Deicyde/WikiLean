#!/usr/bin/env python3
"""Import atomic companion shards into an isolated, local Brain SQLite projection.

The reference FCA package owns validation and discovery. This adapter owns only
transport provenance and a conservative association projection: raw URI endpoints,
no invented declarations/concepts, and no formalization or review claims.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
import builtins
import ctypes
import hashlib
import importlib
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


def _require_reference():
    try:
        import formal_companion_annotations as fca
        if getattr(fca, "__version__", None) != REFERENCE_VERSION:
            raise ImportError(
                f"FCA {REFERENCE_VERSION} is required for this draft integration; "
                f"found {getattr(fca, '__version__', 'unknown')}. "
                "See docs/COMPANION-IMPORT.md."
            )
        companion = importlib.import_module("formal_companion_annotations.companion")
        discovery = importlib.import_module("formal_companion_annotations.discovery")
    except builtins.ImportError as exc:
        raise ImportError(
            "Install the FCA 2.0.0a2 reference package in this Python environment; "
            "see docs/COMPANION-IMPORT.md. Validation is required, never bypassed."
        ) from exc
    return companion, discovery


def _collect(paths, *, include, exclude):
    _, discovery = _require_reference()
    return discovery.collect(paths, include=include, exclude=exclude)


def _validate_captured(collection):
    """Copy and revalidate in memory without reopening or resolving carriers."""
    if collection.problems or not collection.records:
        return collection  # The shared projection path reports fatal batch findings.
    reference, discovery = _require_reference()
    copied = discovery.Collection(files=list(collection.files))
    for record in collection.records:
        if not isinstance(record.path, Path) or not record.path.is_absolute():
            raise ImportError("Captured records require an absolute carrier Path")
        if not isinstance(record.pointer, str):
            raise ImportError("Captured records require a JSON pointer string")
        if record.line is not None and (type(record.line) is not int or record.line < 1):
            raise ImportError("Captured JSONL line must be a positive integer")
        try:
            annotation = reference.loads(reference.dumps(record.annotation))
        except ValueError as exc:
            raise ImportError(f"Invalid captured annotation at {record.path}: {exc}") from exc
        findings = reference.check(annotation).problems
        if findings:
            raise ImportError("Invalid captured annotation; nothing imported: " +
                              "; ".join(str(problem) for problem in findings))
        copied.records.append(discovery.Record(annotation, record.path, record.pointer, record.line))
    return copied


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
    try:
        urlsplit(base_uri)
    except ValueError:
        digest = hashlib.sha256(_json([base_uri, reference]).encode()).hexdigest()
        return "urn:wikilean:companion:unresolved:" + digest, {"status": "unresolved", "reason": "unsupported_carrier_syntax"}
    base = REFERENCE_PARTS.fullmatch(base_uri).groupdict()
    if base["authority"] is None and not base["path"].startswith("/"):
        digest = hashlib.sha256(_json([base_uri, reference]).encode()).hexdigest()
        return "urn:wikilean:companion:unresolved:" + digest, {"status": "unresolved", "reason": "opaque_carrier_base"}
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
    try:
        urlsplit(key)
    except ValueError:
        digest = hashlib.sha256(_json([base_uri, reference]).encode()).hexdigest()
        return "urn:wikilean:companion:unresolved:" + digest, {"status": "unresolved", "reason": "unsupported_authority_syntax"}
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
        # Discovery already captured the resolved absolute carrier path. Do not
        # resolve it again: later directory/symlink changes cannot rewrite origin.
        if hasattr(record, "origin"):
            origin = record.origin
        else:
            origin = {"base_uri": record.path.as_uri(), "pointer": record.pointer}
            if record.line is not None:
                origin["line"] = record.line
        base_uri = origin["base_uri"]
        encoded = _json(annotation)
        # Origin+ordinal identifies an occurrence, not mathematical identity.
        # Repeated IDs and even repeated content remain separate edge rows.
        identity = hashlib.sha256(_json([source_key, origin, encoded]).encode()).hexdigest()
        source, source_resolution = _resolve_reference(base_uri, annotation["source"]["uri"])
        target, target_resolution = _resolve_reference(base_uri, annotation["target"])
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
    _preflight(source_key, output)
    collection = _collect(paths, include=include, exclude=exclude)
    return import_collected(collection, source_key=source_key, output=output)


def _preflight(source_key, output):
    if not isinstance(source_key, str) or not SOURCE_KEY.fullmatch(source_key):
        raise ImportError("source-key must be 1–128 letters/digits/dots/underscores/hyphens, starting with a letter or digit")
    target = Path(output).absolute()
    if target.exists() or target.is_symlink():
        raise ImportError(f"Output already exists: {target}")
    return target


def import_collected(collection, *, source_key: str, output: Path | str) -> dict[str, Any]:
    """Publish one captured FCA Collection without rediscovering source files.

    Every annotation is copied and revalidated with the reference checker. Carrier
    paths are the already captured absolute paths, never resolved again. A caller
    can pass the same collection to several consumers for a coherent generation.
    """
    collection = _validate_captured(collection)
    return _publish_collection(collection, source_key=source_key, output=output)


def import_retained(entries, *, source_key: str, output: Path | str) -> dict[str, Any]:
    """Read native annotations with durable carrier bases, including HTTP bases.

    This operational envelope is not part of FCA. Core values are validated by
    the reference package and retained unchanged; no native-to-legacy projection.
    """
    _preflight(source_key, output)
    reference, _ = _require_reference()
    records = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"annotation", "origin"}:
            raise ImportError("Expected retained annotation and origin")
        value = reference.loads(reference.dumps(entry))
        origin = value["origin"]
        if not isinstance(origin, dict) or not isinstance(origin.get("base_uri"), str) or not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", origin["base_uri"]) or re.search(r"\s", origin["base_uri"]):
            raise ImportError("Retained origin requires an absolute carrier base_uri")
        if reference.check({"source": {"type": "generic", "uri": origin["base_uri"]}, "target": "urn:wikilean:origin-validation"}).problems:
            raise ImportError("Retained carrier base_uri is not a valid URI reference")
        if not isinstance(origin.get("pointer"), str) or (origin["pointer"] and not origin["pointer"].startswith("/")) or re.search(r"~(?![01])", origin["pointer"]):
            raise ImportError("Retained origin requires a JSON pointer with valid escapes")
        if "path" in origin and (not isinstance(origin["path"], str) or not origin["path"]):
            raise ImportError("Retained origin path must be a nonempty string when supplied")
        if origin.get("line") is not None and (type(origin["line"]) is not int or origin["line"] < 1):
            raise ImportError("Retained origin line must be positive")
        findings = reference.check(value["annotation"]).problems
        if findings:
            raise ImportError("Invalid retained annotation: " + "; ".join(str(p) for p in findings))
        records.append(SimpleNamespace(annotation=value["annotation"], origin=origin))
    collection = SimpleNamespace(records=records, problems=[], files=sorted({r.origin["base_uri"] for r in records}))
    return _publish_collection(collection, source_key=source_key, output=output)


def _publish_collection(collection, *, source_key, output):
    target = _preflight(source_key, output)
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
