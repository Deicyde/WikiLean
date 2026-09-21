#!/usr/bin/env python3
"""Filesystem primitives for fail-closed sealed-replay stages.

Prepared replay workspaces are private, but their modes are not a security
boundary.  These helpers provide deterministic modes, durable directory
creation, owned scratch cleanup, and atomic no-replace file publication.  The
full replay runner is still responsible for read-only input/code mounts and an
isolated writable output workspace.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(slots=True)
class OwnedDirectory:
    """A directory this process created and may therefore remove safely."""

    path: Path
    device: int
    inode: int
    removed: bool = False


def fsync_directory(path: Path) -> None:
    """Synchronize one real directory without following its final component."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(f"not a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _relative_parts(root: Path, destination: Path) -> tuple[str, ...]:
    root = Path(root)
    destination = Path(destination)
    if not root.is_absolute() or not destination.is_absolute():
        raise ValueError("stage roots and destinations must be absolute")
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"stage path escapes its root: {destination}") from exc
    if ".." in relative.parts:
        raise ValueError(f"stage path escapes its root: {destination}")
    return relative.parts


def _require_real_directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"context directory is not usable: {path}")
    return metadata


def ensure_private_directory(root: Path, destination: Path) -> Path:
    """Create a 0700 directory chain beneath an existing context root."""
    root = Path(root)
    destination = Path(destination)
    parts = _relative_parts(root, destination)
    _require_real_directory(root)
    current = root
    created: list[Path] = []
    try:
        for part in parts:
            parent = current
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                _require_real_directory(current)
            else:
                current.chmod(0o700)
                created.append(current)
                fsync_directory(current)
                fsync_directory(parent)
        return destination
    except BaseException as exc:
        for directory in reversed(created):
            try:
                directory.rmdir()
                fsync_directory(directory.parent)
            except OSError as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"directory cleanup also failed for {directory}: {cleanup_error}"
                    )
        raise


def create_owned_directory(root: Path, destination: Path) -> OwnedDirectory:
    """Create one fresh private stage directory beneath ``root``."""
    root = Path(root)
    destination = Path(destination)
    _relative_parts(root, destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"stage scratch path already exists: {destination}")
    ensure_private_directory(root, destination.parent)
    ownership: OwnedDirectory | None = None
    try:
        destination.mkdir(mode=0o700)
        metadata = _require_real_directory(destination)
        ownership = OwnedDirectory(destination, metadata.st_dev, metadata.st_ino)
        destination.chmod(0o700)
        fsync_directory(destination)
        fsync_directory(destination.parent)
    except BaseException:
        if ownership is not None and _still_owned(ownership):
            try:
                destination.rmdir()
                fsync_directory(destination.parent)
            except OSError:
                pass
        raise
    assert ownership is not None
    return ownership


def _still_owned(directory: OwnedDirectory) -> bool:
    try:
        metadata = directory.path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_dev == directory.device
        and metadata.st_ino == directory.inode
    )


def remove_owned_directory(directory: OwnedDirectory) -> None:
    """Remove a stage directory only while it is still the inode we created."""
    if directory.removed:
        return
    if not directory.path.exists() and not directory.path.is_symlink():
        directory.removed = True
        return
    if not _still_owned(directory):
        raise RuntimeError(
            f"refusing to clean a replaced stage directory: {directory.path}"
        )
    shutil.rmtree(directory.path)
    fsync_directory(directory.path.parent)
    directory.removed = True


@contextmanager
def owned_directory(root: Path, destination: Path) -> Iterator[OwnedDirectory]:
    """Yield a fresh stage directory and remove it on every exit path."""
    ownership = create_owned_directory(root, destination)
    try:
        yield ownership
    except BaseException as exc:
        try:
            remove_owned_directory(ownership)
        except BaseException as cleanup_error:
            if hasattr(exc, "add_note"):
                exc.add_note(f"stage scratch cleanup also failed: {cleanup_error}")
        raise
    else:
        remove_owned_directory(ownership)


def write_bytes_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    """Create and durably write one regular file with a deterministic mode."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def require_same_filesystem(left: Path, right: Path) -> None:
    """Fail before expensive reduction when publication cannot use hard links."""
    left_metadata = _require_real_directory(Path(left))
    right_metadata = _require_real_directory(Path(right))
    if left_metadata.st_dev != right_metadata.st_dev:
        raise OSError(
            errno.EXDEV,
            "stage scratch and output must be on the same filesystem",
            f"{left} -> {right}",
        )


def assert_outputs_absent(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Reject an already-populated stage output before creating scratch state."""
    outputs = tuple(Path(path) for path in paths)
    if len(outputs) != len(set(outputs)):
        raise ValueError("stage outputs must be distinct")
    for output in outputs:
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"context-owned output already exists: {output}")
    return outputs


def _regular_metadata(path: Path) -> os.stat_result:
    initial = path.lstat()
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise OSError(f"stage publication source is not a regular file: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(initial, metadata)
            or not os.path.samestat(metadata, current)
        ):
            raise OSError(f"stage publication source is not a stable regular file: {path}")
        os.fsync(descriptor)
        return metadata
    finally:
        os.close(descriptor)


def _unlink_owned_file(path: Path, metadata: os.stat_result) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode) or not os.path.samestat(current, metadata):
        raise RuntimeError(f"refusing to unlink replaced stage output: {path}")
    path.unlink()


def publish_files_no_replace(
    publications: Iterable[tuple[Path, Path]],
    *,
    scratch: OwnedDirectory,
) -> tuple[Path, ...]:
    """Atomically link complete files into place, rolling back the whole set.

    Sources must live on the same filesystem as their destinations.  Prepared
    replay workspaces satisfy that condition; checking it explicitly avoids a
    late ``EXDEV`` after an expensive SQLite build.  The owned scratch tree is
    removed before success is reported.  If publication, fsync, or cleanup
    fails, every output still owned by this call is removed and synchronized.
    """
    pairs = tuple(
        (Path(source), Path(destination)) for source, destination in publications
    )
    if not pairs:
        raise ValueError("stage publication must contain at least one file")
    scratch_metadata = _require_real_directory(scratch.path)
    if (
        scratch.removed
        or scratch_metadata.st_dev != scratch.device
        or scratch_metadata.st_ino != scratch.inode
    ):
        raise RuntimeError(
            f"refusing to publish from a replaced stage directory: {scratch.path}"
        )
    destinations = assert_outputs_absent(destination for _source, destination in pairs)
    source_metadata: dict[Path, os.stat_result] = {}
    output_directories: list[Path] = []
    for source, destination in pairs:
        metadata = _regular_metadata(source)
        parent = destination.parent
        parent_metadata = _require_real_directory(parent)
        if metadata.st_dev != parent_metadata.st_dev:
            raise OSError(
                errno.EXDEV,
                "stage scratch and output must be on the same filesystem",
                f"{source} -> {destination}",
            )
        source_metadata[source] = metadata
        if parent not in output_directories:
            output_directories.append(parent)

    published: list[tuple[Path, os.stat_result]] = []
    try:
        for source, destination in pairs:
            os.link(source, destination, follow_symlinks=False)
            published.append((destination, source_metadata[source]))
            linked = destination.lstat()
            if not os.path.samestat(source_metadata[source], linked):
                raise RuntimeError(
                    f"published output does not match its source: {destination}"
                )
        for directory in output_directories:
            fsync_directory(directory)
        remove_owned_directory(scratch)
        return destinations
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination, metadata in reversed(published):
            try:
                _unlink_owned_file(destination, metadata)
            except BaseException as rollback_error:
                rollback_errors.append(f"{destination}: {rollback_error}")
        for directory in output_directories:
            try:
                fsync_directory(directory)
            except BaseException as rollback_error:
                rollback_errors.append(f"fsync {directory}: {rollback_error}")
        try:
            remove_owned_directory(scratch)
        except BaseException as cleanup_error:
            rollback_errors.append(f"scratch cleanup: {cleanup_error}")
        if rollback_errors and hasattr(exc, "add_note"):
            exc.add_note("stage publication rollback issues: " + "; ".join(rollback_errors))
        raise


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an existing destination."""
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    result: int
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        renamex_np = library.renamex_np
        renamex_np.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        renameat2 = library.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, destination_bytes, 0x00000001)
    elif os.name == "nt":
        os.rename(source, destination)
        return
    else:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks atomic no-replace directory publication",
            str(destination),
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            "context-owned output already exists",
            str(destination),
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _tree_entry(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_ctime_ns,
    )


def _snapshot_directory_tree(
    root: Path,
    *,
    synchronize: bool,
) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Validate a regular-file tree, optionally syncing it, and record identity."""
    state: dict[str, tuple[int, int, int, int, int, int]] = {}
    directories: list[Path] = []

    def fail_walk(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(
        root,
        topdown=True,
        onerror=fail_walk,
        followlinks=False,
    ):
        directory_path = Path(directory)
        directory_metadata = _require_real_directory(directory_path)
        if stat.S_IMODE(directory_metadata.st_mode) != 0o700:
            raise OSError(
                f"stage output directory must have mode 0o700: {directory_path}"
            )
        directories.append(directory_path)
        names.sort()
        filenames.sort()
        for name in names:
            child = directory_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError(f"stage output tree contains a non-directory: {child}")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise OSError(f"stage output directory must have mode 0o700: {child}")
            state[child.relative_to(root).as_posix()] = _tree_entry(metadata)
        for name in filenames:
            child = directory_path / name
            metadata = _regular_metadata(child)
            if stat.S_IMODE(metadata.st_mode) != 0o644:
                raise OSError(f"stage output file must have mode 0o644: {child}")
            state[child.relative_to(root).as_posix()] = _tree_entry(metadata)
    if synchronize:
        for directory in reversed(directories):
            fsync_directory(directory)
    return state


def publish_directory_no_replace(
    scratch: OwnedDirectory,
    destination: Path,
) -> Path:
    """Atomically publish one complete owned directory without replacement."""
    destination = assert_outputs_absent([destination])[0]
    destination_parent = destination.parent
    source_metadata = _require_real_directory(scratch.path)
    if (
        scratch.removed
        or source_metadata.st_dev != scratch.device
        or source_metadata.st_ino != scratch.inode
    ):
        raise RuntimeError(
            f"refusing to publish a replaced stage directory: {scratch.path}"
        )
    parent_metadata = _require_real_directory(destination_parent)
    if source_metadata.st_dev != parent_metadata.st_dev:
        raise OSError(
            errno.EXDEV,
            "stage scratch and output must be on the same filesystem",
            f"{scratch.path} -> {destination}",
        )
    source_state = _snapshot_directory_tree(scratch.path, synchronize=True)
    moved = False
    try:
        _rename_no_replace(scratch.path, destination)
        moved = True
        current = destination.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or not os.path.samestat(source_metadata, current)
        ):
            raise RuntimeError(
                f"published output directory does not match its source: {destination}"
            )
        if _snapshot_directory_tree(destination, synchronize=False) != source_state:
            raise RuntimeError(
                f"published output tree changed during publication: {destination}"
            )
        fsync_directory(destination_parent)
        if scratch.path.parent != destination_parent:
            fsync_directory(scratch.path.parent)
        scratch.removed = True
        return destination
    except BaseException as exc:
        rollback_errors: list[str] = []
        if moved:
            try:
                current = destination.lstat()
                if not (
                    stat.S_ISDIR(current.st_mode)
                    and os.path.samestat(source_metadata, current)
                ):
                    raise RuntimeError(
                        f"refusing to remove replaced stage output: {destination}"
                    )
                _rename_no_replace(destination, scratch.path)
                restored = scratch.path.lstat()
                if (
                    not stat.S_ISDIR(restored.st_mode)
                    or not os.path.samestat(source_metadata, restored)
                ):
                    raise RuntimeError(
                        f"rolled-back stage directory changed identity: {scratch.path}"
                    )
                remove_owned_directory(scratch)
            except BaseException as rollback_error:
                rollback_errors.append(f"{destination}: {rollback_error}")
            for parent in dict.fromkeys((destination_parent, scratch.path.parent)):
                try:
                    fsync_directory(parent)
                except BaseException as rollback_error:
                    rollback_errors.append(f"fsync {parent}: {rollback_error}")
        if rollback_errors and hasattr(exc, "add_note"):
            exc.add_note(
                "stage directory publication rollback issues: "
                + "; ".join(rollback_errors)
            )
        raise


__all__ = [
    "OwnedDirectory",
    "assert_outputs_absent",
    "create_owned_directory",
    "ensure_private_directory",
    "fsync_directory",
    "owned_directory",
    "publish_directory_no_replace",
    "publish_files_no_replace",
    "remove_owned_directory",
    "require_same_filesystem",
    "write_bytes_exclusive",
]
