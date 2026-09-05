#!/usr/bin/env python3
"""Canonical, crash-safe publication helpers for Wikidata acquisition jobs."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

FORCE_ENV = "BRAIN_INGEST_FORCE"


def force_publish_enabled() -> bool:
    """Whether the operator explicitly approved bypassing volume guards."""
    return os.environ.get(FORCE_ENV) == "1"


def conservative_volume_floor(previous_count: int) -> int:
    """Match the repository's ingest floor: half, at least 50, capped at prior."""
    if (
        isinstance(previous_count, bool)
        or not isinstance(previous_count, int)
        or previous_count < 0
    ):
        raise ValueError("previous_count must be a nonnegative integer")
    if previous_count == 0:
        return 0
    return min(max(50, previous_count // 2), previous_count)


def require_volume(*, artifact: str, actual: int, floor: int) -> None:
    """Reject a suspicious collapse unless the operator explicitly forced it."""
    if force_publish_enabled() or actual >= floor:
        return
    raise RuntimeError(
        f"refusing to replace {artifact}: candidate count {actual} is below "
        f"the sanity floor {floor}; set {FORCE_ENV}=1 only after reviewing "
        "the intentional source-volume change"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Encode JSON with one stable representation and a final newline."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_jsonl_bytes(rows: Iterable[object]) -> bytes:
    """Encode rows canonically in the caller-supplied canonical order."""
    return b"".join(canonical_json_bytes(row) for row in rows)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Durably replace ``path`` with complete bytes from a sibling staging file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        temporary.unlink(missing_ok=True)
