#!/usr/bin/env python3
"""Materialize a verified sealed D1 article snapshot into the disk cache.

D1 remains canonical.  This command never queries or writes D1: it accepts one
explicit content-addressed bundle produced by ``brain/acquire_d1_snapshot.py``,
verifies the complete acquisition/normalization evidence chain, and mirrors its
article rows into ``site/annotations``.  The whole cache directory is prepared
off to the side and atomically exchanged, so readers see either the previous
generation or the complete new generation.
"""
from __future__ import annotations

import argparse
import ctypes
import decimal
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
for path in (BRAIN, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import authority_contracts as contracts  # noqa: E402
import stage_io  # noqa: E402
from d1_snapshot_bundle import (  # noqa: E402
    SnapshotBundle,
    SnapshotBundleError,
    verify_snapshot_bundle,
)

DEFAULT_ANNOTATIONS = ROOT / "site" / "annotations"
MANIFEST_NAME = ".d1_pull_manifest.json"
MANIFEST_SCHEMA = "wikilean.annotation-mirror/v1"
QUARANTINE_DIR = ".d1_disk_only"
ROOT_DOMAIN = b"wikilean.annotation-mirror.sidecars.v1\0"


class AnnotationMirrorError(RuntimeError):
    """The cache cannot be updated without losing atomicity or provenance."""


@dataclass(frozen=True)
class MirrorPlan:
    files: Mapping[str, bytes]
    source_snapshot: Mapping[str, "TreeEntry"]
    quarantine: Mapping[str, str]
    created: int
    updated: int
    unchanged: int
    disk_only: tuple[str, ...]


@dataclass(frozen=True)
class MirrorResult:
    bundle: SnapshotBundle
    created: int
    updated: int
    unchanged: int
    disk_only: tuple[str, ...]
    dry_run: bool
    cleanup_warning: str | None = None


@dataclass(frozen=True)
class TreeEntry:
    kind: str
    mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _entry(metadata: os.stat_result, kind: str) -> TreeEntry:
    return TreeEntry(
        kind=kind,
        mode=stat.S_IMODE(metadata.st_mode),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _snapshot_tree(root: Path) -> dict[str, TreeEntry]:
    """Capture a no-symlink regular-file tree for race detection and cloning."""
    root = Path(root)
    result: dict[str, TreeEntry] = {}

    def visit(directory: Path, relative: Path) -> None:
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise AnnotationMirrorError(
                f"cannot inspect cache directory {directory}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AnnotationMirrorError(f"cache path is not a real directory: {directory}")
        key = "." if not relative.parts else relative.as_posix()
        result[key] = _entry(metadata, "directory")
        try:
            children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            raise AnnotationMirrorError(
                f"cannot enumerate cache directory {directory}: {exc}"
            ) from exc
        for child in children:
            child_relative = relative / child.name
            try:
                child_meta = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise AnnotationMirrorError(
                    f"cannot inspect cache member {child.path}: {exc}"
                ) from exc
            if stat.S_ISLNK(child_meta.st_mode):
                raise AnnotationMirrorError(f"cache tree contains a symlink: {child.path}")
            if stat.S_ISDIR(child_meta.st_mode):
                visit(Path(child.path), child_relative)
            elif stat.S_ISREG(child_meta.st_mode):
                result[child_relative.as_posix()] = _entry(child_meta, "file")
            else:
                raise AnnotationMirrorError(f"cache tree contains a special file: {child.path}")

    visit(root, Path())
    return result


def _same_tree_after_root_rename(
    actual: Mapping[str, TreeEntry], expected: Mapping[str, TreeEntry]
) -> bool:
    """Compare a moved tree while allowing rename-induced root timestamps."""
    if actual.keys() != expected.keys():
        return False
    for relative, expected_entry in expected.items():
        actual_entry = actual[relative]
        if relative == ".":
            if (
                actual_entry.kind,
                actual_entry.mode,
                actual_entry.device,
                actual_entry.inode,
            ) != (
                expected_entry.kind,
                expected_entry.mode,
                expected_entry.device,
                expected_entry.inode,
            ):
                return False
        elif actual_entry != expected_entry:
            return False
    return True


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise AnnotationMirrorError(f"cache member is not a regular file: {path}")
            chunks: list[bytes] = []
            while block := os.read(descriptor, 1024 * 1024):
                chunks.append(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        linked = path.lstat()
    except AnnotationMirrorError:
        raise
    except OSError as exc:
        raise AnnotationMirrorError(f"cannot read cache member {path}: {exc}") from exc
    if (
        not stat.S_ISREG(after.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or _entry(before, "file") != _entry(after, "file")
        or not os.path.samestat(after, linked)
        or after.st_size != sum(map(len, chunks))
    ):
        raise AnnotationMirrorError(f"cache member changed while being read: {path}")
    return b"".join(chunks)


def _strict_object(data: bytes, location: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise AnnotationMirrorError(f"{location}: duplicate key {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise AnnotationMirrorError(f"{location}: invalid numeric constant {value}")

    try:
        value = json.loads(
            data,
            object_pairs_hook=unique,
            parse_float=decimal.Decimal,
            parse_constant=reject_constant,
        )
    except AnnotationMirrorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationMirrorError(f"{location}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AnnotationMirrorError(f"{location}: expected a JSON object")
    return value


def _slug_filename(slug: Any) -> str:
    if not isinstance(slug, str) or not slug:
        raise AnnotationMirrorError("article slug must be a non-empty string")
    if (
        slug.startswith(".")
        or ".." in slug
        or "/" in slug
        or "\\" in slug
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in slug)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in slug)
    ):
        raise AnnotationMirrorError(f"refusing suspicious article slug {slug!r}")
    filename = f"{slug}.json"
    filename_key = _filename_key(filename)
    if (
        filename_key == "_meta.json"
        or filename_key.endswith(".agent1.json")
        or filename == MANIFEST_NAME
    ):
        raise AnnotationMirrorError(f"article slug collides with a reserved cache name: {slug!r}")
    if len(unicodedata.normalize("NFD", filename).encode("utf-8")) > 255:
        raise AnnotationMirrorError(f"article cache filename exceeds 255 UTF-8 bytes: {slug!r}")
    return filename


def _filename_key(filename: str) -> str:
    return unicodedata.normalize("NFD", filename).casefold()


def _pretty_json(
    value: Any, *, level: int = 0, active: set[int] | None = None
) -> str:
    """Render strict, readable JSON while preserving Decimal values exactly."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if type(value) is int:
        return str(value)
    if isinstance(value, decimal.Decimal):
        return contracts.canonical_artifact_json_bytes(value).decode("ascii")
    if type(value) is float:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if not isinstance(value, (list, dict)):
        raise TypeError(f"unsupported JSON value {type(value).__name__}")

    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise ValueError("circular JSON value")
    active.add(identity)
    try:
        indentation = "  " * level
        child_indentation = "  " * (level + 1)
        if isinstance(value, list):
            if not value:
                return "[]"
            children = [
                child_indentation
                + _pretty_json(item, level=level + 1, active=active)
                for item in value
            ]
            return "[\n" + ",\n".join(children) + "\n" + indentation + "]"

        if not value:
            return "{}"
        members: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            encoded_key = json.dumps(key, ensure_ascii=False, allow_nan=False)
            members.append(
                child_indentation
                + encoded_key
                + ": "
                + _pretty_json(item, level=level + 1, active=active)
            )
        return "{\n" + ",\n".join(members) + "\n" + indentation + "}"
    finally:
        active.remove(identity)


def _pretty_json_bytes(value: Any) -> bytes:
    return (_pretty_json(value) + "\n").encode("utf-8")


def _sidecar_bytes(row: Mapping[str, Any], existing: bytes | None) -> bytes:
    if existing is None:
        model: dict[str, Any] = {
            "slug": row["slug"],
            "wikipedia_title": row["wikipedia_title"],
            "display_title": row["display_title"],
            "schema_version": row["schema_version"],
        }
    else:
        model = _strict_object(existing, f"existing sidecar {row['slug']}.json")
    model["slug"] = row["slug"]
    model["wikipedia_title"] = row["wikipedia_title"]
    model["display_title"] = row["display_title"]
    model["schema_version"] = row["schema_version"]
    model["annotations"] = row["annotations"]
    try:
        return _pretty_json_bytes(model)
    except (
        TypeError,
        ValueError,
        UnicodeEncodeError,
        contracts.VerificationError,
    ) as exc:
        raise AnnotationMirrorError(f"cannot encode sidecar for {row['slug']}: {exc}") from exc


def _sidecars_root(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(ROOT_DOMAIN)
    for entry in entries:
        digest.update(contracts.canonical_json_bytes(entry))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def build_plan(bundle: SnapshotBundle, annotations_dir: Path) -> MirrorPlan:
    """Validate the existing cache and compute every output byte before writing."""
    if not bundle.articles:
        raise AnnotationMirrorError("refusing to mirror a snapshot with zero articles")
    annotations_dir = Path(annotations_dir)
    source_snapshot = _snapshot_tree(annotations_dir)
    existing_root_names = {
        _filename_key(relative): relative
        for relative, entry in source_snapshot.items()
        if entry.kind == "file" and "/" not in relative
    }
    files: dict[str, bytes] = {}
    manifest_rows: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    created = updated = unchanged = 0

    for row in bundle.articles:
        filename = _slug_filename(row["slug"])
        collision_key = _filename_key(filename)
        prior_name = seen_names.get(collision_key)
        if prior_name is not None:
            raise AnnotationMirrorError(
                f"article slugs collide as cache filenames: {prior_name!r} and {filename!r}"
            )
        seen_names[collision_key] = filename
        existing_name = existing_root_names.get(collision_key)
        if existing_name is not None and existing_name != filename:
            raise AnnotationMirrorError(
                f"article cache filename collides with existing member: "
                f"{filename!r} and {existing_name!r}"
            )
        target = annotations_dir / filename
        existing = _read_regular(target) if target.exists() or target.is_symlink() else None
        next_bytes = _sidecar_bytes(row, existing)
        files[filename] = next_bytes
        if existing is None:
            created += 1
        elif existing == next_bytes:
            unchanged += 1
        else:
            updated += 1
        manifest_rows.append(
            {
                "slug": row["slug"],
                "version": row["version"],
                "revid": row["revid"],
                "sha256": hashlib.sha256(next_bytes).hexdigest(),
                "bytes": len(next_bytes),
            }
        )

    manifest_rows.sort(key=lambda item: item["slug"].encode("utf-8"))
    disk_only = tuple(
        sorted(
            (
                path.name[:-5]
                for path in annotations_dir.iterdir()
                if path.is_file()
                and not path.name.startswith(".")
                and path.name.endswith(".json")
                and path.name not in files
            ),
            key=lambda value: value.encode("utf-8"),
        )
    )
    quarantine_generation = (
        f"{QUARANTINE_DIR}/{bundle.normalization_lineage_id.removeprefix('sha256:')}"
    )
    existing_quarantine = {
        relative
        for relative, entry in source_snapshot.items()
        if entry.kind == "file" and relative.startswith(f"{QUARANTINE_DIR}/")
    }
    quarantine: dict[str, str] = {}
    new_quarantine_files = 0
    for slug in disk_only:
        filename = f"{slug}.json"
        destination = f"{quarantine_generation}/{filename}"
        if destination in existing_quarantine:
            if _read_regular(annotations_dir / filename) != _read_regular(
                annotations_dir / destination
            ):
                raise AnnotationMirrorError(
                    f"disk-only quarantine collision for {filename!r}"
                )
        else:
            new_quarantine_files += 1
        quarantine[filename] = destination
    manifest: dict[str, Any] = {
        "_meta": {
            "schema": MANIFEST_SCHEMA,
            "acquisition_receipt_id": bundle.acquisition_receipt_id,
            "normalization_lineage_id": bundle.normalization_lineage_id,
            "acquired_at": bundle.acquired_at,
            "article_count": len(manifest_rows),
            "sidecars_root": _sidecars_root(manifest_rows),
            "quarantined_count": len(existing_quarantine) + new_quarantine_files,
            "quarantine_path": QUARANTINE_DIR,
        }
    }
    for row in manifest_rows:
        manifest[row["slug"]] = {
            "version": row["version"],
            "revid": row["revid"],
            # Backward-compatible field: this is the bundle's acquisition time,
            # never a new wall clock sampled by the mirror command.
            "pulled_at": bundle.acquired_at,
            "sha256": row["sha256"],
            "bytes": row["bytes"],
        }
    files[MANIFEST_NAME] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if _snapshot_tree(annotations_dir) != source_snapshot:
        raise AnnotationMirrorError("annotation cache changed while the mirror plan was built")
    return MirrorPlan(
        files=files,
        source_snapshot=source_snapshot,
        quarantine=quarantine,
        created=created,
        updated=updated,
        unchanged=unchanged,
        disk_only=disk_only,
    )


def _clone_tree(source: Path, destination: Path, snapshot: Mapping[str, TreeEntry]) -> None:
    for relative, entry in sorted(
        snapshot.items(), key=lambda item: (item[0].count("/"), os.fsencode(item[0]))
    ):
        if relative == "." or entry.kind != "directory":
            continue
        target = destination / relative
        target.mkdir(mode=entry.mode)
        target.chmod(entry.mode)
    for relative, entry in sorted(snapshot.items(), key=lambda item: os.fsencode(item[0])):
        if entry.kind != "file":
            continue
        source_file = source / relative
        target_file = destination / relative
        data = _read_regular(source_file)
        current = source_file.lstat()
        if _entry(current, "file") != entry:
            raise AnnotationMirrorError(f"cache member changed while cloning: {source_file}")
        stage_io.write_bytes_exclusive(target_file, data, mode=entry.mode)


def _write_staged_files(
    stage: Path,
    files: Mapping[str, bytes],
    snapshot: Mapping[str, TreeEntry],
) -> None:
    for relative, data in sorted(files.items(), key=lambda item: os.fsencode(item[0])):
        target = stage / relative
        prior = snapshot.get(relative)
        if target.exists() or target.is_symlink():
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise AnnotationMirrorError(
                    f"refusing to replace non-regular cache member: {target}"
                )
            target.unlink()
        stage_io.write_bytes_exclusive(target, data, mode=prior.mode if prior else 0o644)


def _quarantine_disk_only(stage: Path, quarantine: Mapping[str, str]) -> None:
    for source_name, destination_name in sorted(
        quarantine.items(), key=lambda item: os.fsencode(item[0])
    ):
        source = stage / source_name
        destination = stage / destination_name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        (stage / QUARANTINE_DIR).chmod(0o700)
        destination.parent.chmod(0o700)
        if destination.exists() or destination.is_symlink():
            if _read_regular(source) != _read_regular(destination):
                raise AnnotationMirrorError(
                    f"disk-only quarantine collision for {source_name!r}"
                )
            source.unlink()
        else:
            os.rename(source, destination)


def _sync_tree(root: Path) -> None:
    directories: list[Path] = []

    def fail_walk(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(
        root, topdown=True, onerror=fail_walk, followlinks=False
    ):
        directory_path = Path(directory)
        directories.append(directory_path)
        names.sort()
        filenames.sort()
        for name in filenames:
            path = directory_path / name
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise AnnotationMirrorError(f"staged cache member is not regular: {path}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        stage_io.fsync_directory(directory)


def _exchange_directories(left: Path, right: Path) -> None:
    """Atomically swap two existing directories or fail without changing either."""
    library = ctypes.CDLL(None, use_errno=True)
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        operation = library.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(left_bytes, right_bytes, 0x00000002)  # RENAME_SWAP
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
        result = operation(-100, left_bytes, -100, right_bytes, 0x00000002)
    else:
        raise OSError(errno.ENOTSUP, "platform lacks atomic directory exchange")
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), f"{left} <-> {right}")


@contextmanager
def _publication_lock(annotations_dir: Path) -> Iterator[None]:
    lock_path = annotations_dir.parent / f".{annotations_dir.name}.pull.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        linked = lock_path.lstat()
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, linked):
            raise AnnotationMirrorError(
                f"annotation mirror lock is not a regular file: {lock_path}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = os.fstat(descriptor)
        linked = lock_path.lstat()
        if (
            not stat.S_ISREG(locked.st_mode)
            or not os.path.samestat(opened, locked)
            or not os.path.samestat(locked, linked)
        ):
            raise AnnotationMirrorError(
                f"annotation mirror lock was replaced while waiting: {lock_path}"
            )
        yield
    finally:
        os.close(descriptor)


def _publish(annotations_dir: Path, plan: MirrorPlan) -> str | None:
    parent = annotations_dir.parent
    with _publication_lock(annotations_dir):
        original = _snapshot_tree(annotations_dir)
        if original != plan.source_snapshot:
            raise AnnotationMirrorError(
                "annotation cache changed after the mirror plan was built"
            )
        stage = parent / f".{annotations_dir.name}.pull.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        ownership = stage_io.create_owned_directory(parent, stage)
        exchanged = False
        original_root = annotations_dir.lstat()
        try:
            _clone_tree(annotations_dir, stage, original)
            _write_staged_files(stage, plan.files, original)
            _quarantine_disk_only(stage, plan.quarantine)
            stage.chmod(original["."].mode)
            _sync_tree(stage)
            if _snapshot_tree(annotations_dir) != original:
                raise AnnotationMirrorError(
                    "annotation cache changed while the next generation was staged"
                )
            _exchange_directories(annotations_dir, stage)
            exchanged = True
            try:
                if not _same_tree_after_root_rename(_snapshot_tree(stage), original):
                    raise AnnotationMirrorError(
                        "annotation cache changed during the atomic exchange"
                    )
                stage_io.fsync_directory(parent)
            except BaseException as commit_error:
                try:
                    _exchange_directories(annotations_dir, stage)
                    exchanged = False
                    stage_io.fsync_directory(parent)
                except BaseException as rollback_error:
                    raise AnnotationMirrorError(
                        "annotation cache exchange committed but durability failed, "
                        f"and rollback also failed: {rollback_error}"
                    ) from commit_error
                raise
        except BaseException:
            if not exchanged:
                try:
                    stage_io.remove_owned_directory(ownership)
                except BaseException:
                    pass
            raise

        try:
            old_root = stage.lstat()
            if not os.path.samestat(original_root, old_root):
                return f"refusing to remove unexpected old-cache path {stage}"
            shutil.rmtree(stage)
            stage_io.fsync_directory(parent)
        except OSError as exc:
            return f"new cache committed; old cache remains at {stage}: {exc}"
    return None


def pull(
    snapshot_bundle: Path,
    *,
    annotations_dir: Path = DEFAULT_ANNOTATIONS,
    dry_run: bool = False,
) -> MirrorResult:
    snapshot_bundle = Path(snapshot_bundle)
    annotations_dir = Path(annotations_dir)
    if not snapshot_bundle.is_absolute():
        raise AnnotationMirrorError("snapshot bundle path must be absolute")
    if not annotations_dir.is_absolute():
        raise AnnotationMirrorError("annotation cache path must be absolute")
    bundle = verify_snapshot_bundle(snapshot_bundle)
    plan = build_plan(bundle, annotations_dir)
    warning = None if dry_run else _publish(annotations_dir, plan)
    return MirrorResult(
        bundle=bundle,
        created=plan.created,
        updated=plan.updated,
        unchanged=plan.unchanged,
        disk_only=plan.disk_only,
        dry_run=dry_run,
        cleanup_warning=warning,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-bundle",
        type=Path,
        required=True,
        help="absolute sealed bundle directory produced by acquire_d1_snapshot.py",
    )
    parser.add_argument("--annotations-dir", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.snapshot_bundle.is_absolute():
        parser.error("--snapshot-bundle must be an absolute path")
    if not args.annotations_dir.is_absolute():
        parser.error("--annotations-dir must be an absolute path")
    try:
        result = pull(
            args.snapshot_bundle,
            annotations_dir=args.annotations_dir,
            dry_run=args.dry_run,
        )
    except (AnnotationMirrorError, SnapshotBundleError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(f"verified bundle       : {result.bundle.normalization_lineage_id}")
    print(f"articles mirrored     : {len(result.bundle.articles)}")
    print(f"files created         : {result.created}")
    print(f"files updated         : {result.updated}")
    print(f"files unchanged       : {result.unchanged}")
    if result.disk_only:
        print(f"disk-only sidecars    : {len(result.disk_only)} (quarantined)")
    print(
        "(dry run - cache unchanged)"
        if result.dry_run
        else f"committed atomically  : {args.annotations_dir}"
    )
    if result.cleanup_warning:
        print(f"WARN: {result.cleanup_warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
