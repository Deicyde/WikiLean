"""What the two declaration producers share (`scripts/tauceti_decls`, `scripts/mathlib_decls`):
the pinned inputs (trough at its commit, one doc-gen4 `.bmp`), the name index, dependency
edges, rejected rows, the manifest's logical root and the sealed write.

Producer 1 (`scripts/tauceti_plans`) is the convention; its pin reader, URLs and work-tree
check are reused here, so the three producers pin the same inputs the same way.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

from scripts.brain_producers.common import (
    BuildError,
    GitTree,
    dumps,
    jsonl_bytes,
    sha256_bytes,
    sha256_file,
)
from scripts.tauceti_plans.build import URLS, git_work_tree_holding, logical_root, pinned, read_pins

TROUGH_REPO = "Deicyde/trough"
CLOCK_KEYS = {"fetched_at"}
FILES = ["names.jsonl", "decls.jsonl", "deps.jsonl", "rejected.jsonl"]
DEP_KINDS = (("statement", "uses_in_statement"), ("proof", "uses_in_proof"))
_DOC_LINK = re.compile(r"^\./((?:[^/#]+/)*[^/#]+)\.html#(.+)$")


# ---------------------------------------------------------------- inputs

def refuse_out_in_work_tree(out: str) -> None:
    """The export is never committed: refuse an --out inside a git work tree, before any read."""
    root = git_work_tree_holding(out)
    if root is not None:
        raise BuildError(f"--out {out} lies inside the git work tree {root}; write the export elsewhere")


def load_trough(inputs: str, pins: dict, lib: str, files: list[str]) -> tuple[GitTree, dict[str, bytes]]:
    """trough at its pinned HEAD, and `data/<lib>/<file>` for each file, read from the commit."""
    want = pinned(pins, "trough", "commit", "trough")
    tree = GitTree(os.path.join(inputs, "trough"))
    if want != tree.commit:
        raise BuildError(f"trough: HEAD {tree.commit} does not match pinned {want}")
    out = {}
    for rel in files:
        path = f"data/{lib}/{rel}"
        if not tree.exists(path):
            raise BuildError(f"trough: {path} missing at {tree.commit}")
        out[rel] = tree.read(path)
    return tree, out


def load_index(inputs: str, pins: dict, name: str, rel: str) -> tuple[str, dict, set[str]]:
    """A doc-gen4 `.bmp` (JSON): its sha256 (checked against the pin), declarations, modules."""
    path = os.path.join(inputs, rel)
    want = pinned(pins, name, "sha256", rel)
    try:
        sha = sha256_file(path)
    except OSError as e:
        raise BuildError(f"{rel}: cannot read it ({e}); run this producer's fetch") from None
    if want != sha:
        raise BuildError(f"{rel}: sha256 {sha} does not match pinned {want}")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise BuildError(f"{rel}: not JSON ({e})") from None
    decls = d.get("declarations")
    if not isinstance(decls, dict):
        raise BuildError(f"{rel}: no declarations object")
    return sha, decls, set(d.get("modules") or {})


def docs_commit(pins: dict, name: str) -> str | None:
    c = (pins.get(name) or {}).get("docs_commit")
    return c if isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c) else None


def module_of(doc_link) -> str | None:
    """`./TauCeti/A/B.html#X` -> `TauCeti.A.B`; None for any other shape."""
    m = _DOC_LINK.match(doc_link) if isinstance(doc_link, str) else None
    return m.group(1).replace("/", ".") if m else None


# ---------------------------------------------------------------- rows

def decl_id(library: str, name: str) -> str:
    """brain/SCHEMA.md's decl id: `decl:<Library>:<FQ name>`, the library being the index's."""
    return f"decl:{library}:{name}"


def name_rows(index: dict, library: str, rejected: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """One row per declaration in the index; returns the rows and name -> module."""
    rows, modules = [], {}
    for name in sorted(index):
        entry = index[name]
        link = entry.get("docLink") if isinstance(entry, dict) else None
        mod = module_of(link)
        if mod is None:
            rejected.append({"kind": "name", "reason": "doc_link_unparseable", "candidate": name})
            continue
        modules[name] = mod
        kind = entry.get("kind")
        rows.append({"id": decl_id(library, name), "name": name, "module": mod,
                     "kind": kind if isinstance(kind, str) else None, "doc_link": link})
    return rows, modules


def source_pointer(repos: dict[str, str], commit: str | None, module: str, line) -> dict | None:
    """{repo, commit, path, line} into the repository whose root holds `module`'s first
    component (`repos`, e.g. {"TauCeti": "TauCetiProject/TauCeti"}), at `commit`."""
    repo = repos.get(module.split(".")[0])
    if repo is None or commit is None or not isinstance(line, int) or isinstance(line, bool):
        return None
    return {"repo": repo, "commit": commit, "path": module.replace(".", "/") + ".lean", "line": line}


def trough_fields(name: str, info: dict | None, bmp_module: str, rejected: list[dict], *,
                  repos: dict[str, str], commit: str | None, extra: tuple[str, ...] = ()) -> dict:
    """`line`, `signature`, `doc` (and `extra` keys) from trough's record of the declaration,
    and a `source` pointer. All null when trough has no record, or when trough places it in
    another module than the index does (then a `module_mismatch` rejection)."""
    keys = ("line", "signature", "doc") + extra
    empty = {k: None for k in keys} | {"source": None}
    if not isinstance(info, dict):
        return empty
    if info.get("module") != bmp_module:
        rejected.append({"kind": "decl", "reason": "module_mismatch", "candidate": name,
                         "module": bmp_module, "trough_module": info.get("module")})
        return empty
    out = {}
    for k in keys:
        v = info.get(k)
        out[k] = v if k != "line" or (isinstance(v, int) and not isinstance(v, bool)) else None
    out["source"] = source_pointer(repos, commit, bmp_module, out["line"])
    return out


def dep_edges(records: dict, mapped: dict[str, str], evidence: dict, rejected: list[dict]) -> list[dict]:
    """Edges from each mapped declaration's trough record: its `statement` names give
    `uses_in_statement`, its `proof` names `uses_in_proof`. `source` is the consumer, `target`
    the prerequisite (brain/SCHEMA.md's direction); both must be mapped (`mapped`: name -> id)."""
    edges = {}
    for name in sorted(records):
        info = records[name]
        if name not in mapped or not isinstance(info, dict):
            continue
        for field, kind in DEP_KINDS:
            targets = info.get(field) or []
            for tgt in sorted(set(t for t in targets if isinstance(t, str))):
                base = {"kind": "dep", "dep_kind": kind, "source": mapped[name], "candidate": tgt,
                        "evidence": evidence}
                if tgt == name:
                    rejected.append({**base, "reason": "self_edge"})
                elif tgt not in mapped:
                    rejected.append({**base, "reason": "target_not_mapped"})
                else:
                    eid = f"{kind}:{mapped[name]}->{mapped[tgt]}"
                    edges[eid] = {"id": eid, "kind": kind, "source": mapped[name], "target": mapped[tgt],
                                  "evidence": evidence}
    return list(edges.values())


def rejected_rows(rejected: list[dict]) -> list[dict]:
    """As producer 1: id `<kind>:<first 20 hex of the row's sha256>`, identical rows counted."""
    rows: dict[str, dict] = {}
    for r in rejected:
        rid = f"{r['kind']}:{sha256_bytes(dumps(r).encode())[:20]}"
        if rid in rows:
            rows[rid]["count"] += 1
        else:
            rows[rid] = {"id": rid, **r, "count": 1}
    return list(rows.values())


def count(rows, key) -> dict:
    return dict(sorted(Counter(key(r) for r in rows).items()))


# ---------------------------------------------------------------- manifest and write

def trough_input(tree: GitTree, pins: dict, lib: str, files: dict[str, bytes], **extra) -> dict:
    return {"url": URLS["trough"], "repo": TROUGH_REPO, "commit": tree.commit,
            "fetched_at": (pins.get("trough") or {}).get("fetched_at"),
            "files": {f"data/{lib}/{k}": sha256_bytes(v) for k, v in sorted(files.items())}, **extra}


def index_input(pins: dict, name: str, rel: str, sha: str) -> dict:
    return {"url": URLS[name], "path": rel, "sha256": sha, "docs_commit": docs_commit(pins, name),
            "fetched_at": (pins.get(name) or {}).get("fetched_at")}


def seal(out: str, data: dict[str, bytes], manifest: dict) -> dict:
    """Adds `files`, `clock_fields` and `logical_root`, then writes every file atomically."""
    manifest["files"] = {name: {"rows": blob.count(b"\n"), "sha256": sha256_bytes(blob), "bytes": len(blob)}
                         for name, blob in sorted(data.items())}
    manifest["clock_fields"] = sorted(CLOCK_KEYS)
    manifest["logical_root"] = logical_root(manifest)
    os.makedirs(out, exist_ok=True)
    blobs = dict(data)
    blobs["manifest.json"] = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1,
                                         allow_nan=False) + "\n").encode("utf-8")
    for name, blob in blobs.items():
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, os.path.join(out, name))
    return manifest


__all__ = ["BuildError", "CLOCK_KEYS", "DEP_KINDS", "FILES", "TROUGH_REPO", "URLS", "count", "decl_id",
           "dep_edges", "docs_commit", "index_input", "jsonl_bytes", "load_index", "load_trough",
           "module_of", "name_rows", "read_pins", "refuse_out_in_work_tree", "rejected_rows", "seal",
           "source_pointer", "trough_fields", "trough_input"]
