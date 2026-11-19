"""Canonical JSON and hashing, shared by fetch and build.

The same rules as scripts/tauceti_plans/canon.py: keys sorted, `ensure_ascii=False`,
separators `(",",":")`, LF, one row per line. Two builds of the same inputs are
byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonl_bytes(rows: Iterable[dict], key: Callable[[dict], object]) -> bytes:
    """Rows sorted by `key`, which must be unique: a duplicate is a build error."""
    rows = sorted(rows, key=key)  # type: ignore[arg-type]
    keys = [key(r) for r in rows]
    if len(set(map(repr, keys))) != len(keys):
        raise ValueError("duplicate row key")
    return "".join(dumps(r) + "\n" for r in rows).encode("utf-8")


def read_json(path: str):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def git_work_tree_holding(path: str) -> str | None:
    """The git work tree `path` would lie in (its nearest ancestor holding `.git`), if any."""
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def write_bytes(path: str, blob: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
