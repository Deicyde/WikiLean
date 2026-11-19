"""Canonical JSON, hashing and the git blob reader shared by fetch and build."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

ROADMAP_REPO = "TauCetiProject/TauCetiRoadmap"


class BuildError(Exception):
    """A bad or missing input, reported by `build` as `build failed: …` rather than a traceback."""


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonl_bytes(rows: list[dict]) -> bytes:
    rows = sorted(rows, key=lambda r: r["id"])
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate row id")
    return "".join(dumps(r) + "\n" for r in rows).encode("utf-8")


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: str, *args: str, data: bytes | None = None, env: dict | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", repo, "-c", "core.quotepath=off", *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        env=env if env is not None else _git_env(),
    ).stdout


class GitTree:
    """Reads files exactly as committed at HEAD, so a dirty working tree cannot leak in.

    Reading is offline: `fetch` clones with `--filter=blob:none`, so a blob outside the sparse
    set is absent, and git would lazily fetch it from the remote. `GIT_NO_LAZY_FETCH=1` stops
    that, and the missing blob becomes a BuildError naming the file.
    """

    def __init__(self, repo: str):
        self.repo = repo
        try:
            self.commit = git(repo, "rev-parse", "HEAD").decode().strip()
            out = git(repo, "ls-tree", "-r", "-z", "--full-tree", self.commit)
        except (subprocess.CalledProcessError, OSError) as e:
            raise BuildError(f"{os.path.basename(repo)}: cannot read the checkout at {repo} ({e}); "
                             "run this producer's fetch") from None
        self.blobs: dict[str, str] = {}
        for entry in out.split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            _mode, kind, oid = meta.decode().split(" ")
            if kind == "blob":
                self.blobs[path.decode("utf-8")] = oid

    def exists(self, path: str) -> bool:
        return path in self.blobs

    def read_many(self, paths: list[str]) -> dict[str, bytes]:
        paths = [p for p in paths if p in self.blobs]
        if not paths:
            return {}
        req = "".join(self.blobs[p] + "\n" for p in paths).encode()
        out = git(self.repo, "cat-file", "--batch", data=req,
                  env=_git_env() | {"GIT_NO_LAZY_FETCH": "1"})
        result, pos = {}, 0
        for p in paths:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode().split(" ")
            if len(header) < 3:  # `<oid> missing`: a partial clone without the blob, and no fetch
                raise BuildError(f"{os.path.basename(self.repo)}: {p} is committed at {self.commit} "
                                 f"but its blob {self.blobs[p]} is not in this partial clone; run "
                                 "this producer's fetch")
            size = int(header[2])
            result[p] = out[nl + 1: nl + 1 + size]
            pos = nl + 1 + size + 1
        return result

    def read(self, path: str) -> bytes:
        return self.read_many([path])[path]
