"""Offline, deterministic build of the `wikilean.brain.rh-decls/v1` export.

Reads leanprover-community/riemann-hypothesis at the pinned commit (`rh/` under --inputs,
written by this producer's fetch): its `lakefile.toml`, then every module of the Lake
libraries `lake build` builds (`defaultTargets`). Writes names.jsonl, decls.jsonl, deps.jsonl,
rejected.jsonl and manifest.json under --out. Same inputs and the same reader, same bytes.
Contract: docs/BRAIN-PRODUCERS.md, producer 4.

The repository publishes no doc-gen4 index and trough has no record of it, so the
declarations come from its source text, read by the Brain's own Lean reader: the
frontier-repo harvest (`lean_repo.harvest_rows`, one file at a time) for the name, kind,
statement code and docstring, and `build_common._lean_decl_lines` for the line. That reader
resolves `namespace` blocks and `_root_`, and skips what the source does not spell out
(structure fields, anonymous instances, generated names). No Lean runs, so deps.jsonl is empty.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
import tomllib

from scripts.brain_producers import decls as D
from scripts.brain_producers.common import OFFLINE_ARGS, GitTree, git, offline_env, sha256_file
from scripts.tauceti_plans.build import pinned

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (os.path.join(ROOT, "brain", "ingest"), os.path.join(ROOT, "brain")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import build_common  # noqa: E402  (brain/build_common.py: the Lean declaration reader)
import git_snapshot  # noqa: E402  (brain/ingest/git_snapshot.py: harvest_rows' input type)
import lean_repo  # noqa: E402  (brain/ingest/lean_repo.py: the frontier-repo harvest)

SCHEMA = "wikilean.brain.rh-decls/v1"
PRODUCER = {"name": "scripts.rh_decls", "version": "1.0.0"}
LIBRARY = "RiemannHypothesis"
REPO = "leanprover-community/riemann-hypothesis"
URL = f"https://github.com/{REPO}"
INPUT = "rh"  # the PINS.json entry, and the checkout's folder under --inputs
SRC = "src/"  # what fetch checks out; build takes the source folder from lakefile.toml
FILES = D.FILES
# The reader is an input: a change to any of these files may change the rows, so their
# hashes are in the manifest and hence in the logical root.
READER_FILES = ("brain/build_common.py", "brain/ingest/common.py", "brain/ingest/formal_conjectures.py",
                "brain/ingest/git_snapshot.py", "brain/ingest/lean_repo.py")
READ_CHUNK = 2000  # blobs per `git cat-file --batch`; the repository's src/ is about 1 GB
ATTRIBUTION = [
    {"work": "riemann-hypothesis", "url": URL, "license": None,
     "holder": "the submission's author (see the repository's README)",
     "use": "declaration names, modules, kinds, line numbers, statement code and docstrings, "
            "copied from the source as the Brain's Lean reader extracts them",
     "note": "The README says the submission belongs to its author and was published without a "
             "license. WikiLean's maintainer chose on 2026-11-24 to carry its code and docstrings "
             "(docs/BRAIN-PRODUCERS.md, producer 4)."},
]

_HEADER = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)*((?:{build_common._LEAN_MOD}\s+)*){build_common._LEAN_KW}\s")


def is_private(code: str) -> bool:
    """Whether a declaration line (comments blanked) carries the `private` modifier."""
    m = _HEADER.match(code)
    return bool(m) and "private" in m.group(1).split()


def lean_name(fq: str, module: str, private: bool) -> str:
    """The name Lean gives the declaration: a private one is `_private.<module>.0.<name>`, so
    two modules' private helpers of the same name are two declarations, as in Lean."""
    return f"_private.{module}.0.{fq}" if private else fq


def lake_config(tree: GitTree) -> tuple[str, list[tuple[str, list[tuple[str, str]]]], list[str]]:
    """From lakefile.toml at the pinned commit: the source folder (`srcDir`, as a path prefix),
    the libraries `lake build` builds with their module globs in file order, and the other
    libraries' names. Lake's globs: `X` is the module X, `X.+` its submodules, `X.*` both; a
    library without `globs` has its `roots` (default: its name)."""
    where = f"{INPUT}: lakefile.toml at {tree.commit}"
    if not tree.exists("lakefile.toml"):
        raise D.BuildError(f"{where} is missing")
    try:
        conf = tomllib.loads(tree.read("lakefile.toml").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise D.BuildError(f"{where} is not TOML ({e})") from None
    src = conf.get("srcDir", ".")
    if not isinstance(src, str) or src.startswith("/") or posixpath.normpath(src).startswith(".."):
        raise D.BuildError(f"{where}: srcDir {src!r} is not a folder inside the repository")
    src = posixpath.normpath(src)
    prefix = "" if src == "." else src + "/"
    libs = conf.get("lean_lib") or []
    if not isinstance(libs, list) or not libs:
        raise D.BuildError(f"{where} declares no lean_lib")
    default = conf.get("defaultTargets")
    built, other = [], []
    for lib in libs:
        name = lib.get("name") if isinstance(lib, dict) else None
        if not isinstance(name, str):
            raise D.BuildError(f"{where}: a lean_lib has no name")
        if default is not None and name not in default:
            other.append(name)
            continue
        if "srcDir" in lib:
            raise D.BuildError(f"{where}: lean_lib {name} has its own srcDir, which this producer does not read")
        globs = lib.get("globs") or lib.get("roots") or [name]
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise D.BuildError(f"{where}: lean_lib {name}'s globs are not module names")
        built.append((name, [("submodules", g[:-2]) if g.endswith(".+") else
                             ("and_submodules", g[:-2]) if g.endswith(".*") else ("one", g) for g in globs]))
    if not built:
        raise D.BuildError(f"{where}: no lean_lib is a default target")
    return prefix, built, other


def lean_lib_of(module: str, libs: list[tuple[str, list[tuple[str, str]]]]) -> str | None:
    """The first library (in lakefile order) whose globs take `module`."""
    for name, globs in libs:
        for kind, base in globs:
            if (module == base and kind != "submodules") or (kind != "one" and module.startswith(base + ".")):
                return name
    return None


def file_decls(path: str, prefix: str, text: str, commit: str) -> list[dict]:
    """Every declaration the Brain's reader finds in one source file."""
    rel = path[len(prefix):]
    module = rel[:-len(".lean")].replace("/", ".")
    snap = git_snapshot.GitTextSnapshot(commit=commit, files=(git_snapshot.GitTextFile(rel, text),), tree="")
    rows, _ = lean_repo.harvest_rows(snap, LIBRARY)
    lines = text.splitlines()
    where = build_common._lean_decl_lines(lines)
    code = dict(build_common._strip_lean_comments(lines))
    out = []
    for r in rows:
        idx = where[r["decl"]]
        private = is_private(code[idx])
        out.append({"fq": r["decl"], "name": lean_name(r["decl"], module, private), "module": module,
                    "kind": r.get("kind"), "private": private, "line": idx + 1,
                    "signature": r.get("code"), "doc": r.get("docstring"), "refs": r.get("refs"),
                    "path": path})
    return out


def build(inputs: str, out: str) -> dict:
    D.refuse_out_in_work_tree(out)
    pins = D.read_pins(inputs)
    want = pinned(pins, INPUT, "commit", INPUT)
    tree = GitTree(os.path.join(inputs, INPUT))
    if tree.commit != want:
        raise D.BuildError(f"{INPUT}: HEAD {tree.commit} does not match pinned {want}")
    prefix, libs, other_libs = lake_config(tree)
    paths = sorted(p for p in tree.blobs if p.startswith(prefix) and p.endswith(".lean"))
    rejected: list[dict] = []
    lib_of: dict[str, str] = {}
    for p in paths:
        lib = lean_lib_of(p[len(prefix):-len(".lean")].replace("/", "."), libs)
        if lib is None:
            # In no library `lake build` builds: Comparator's Challenge, which restates the
            # headline theorems with `sorry`, or a file no library takes.
            rejected.append({"kind": "file", "reason": "outside_default_libs", "candidate": p})
        else:
            lib_of[p] = lib
    if not lib_of:
        raise D.BuildError(f"{INPUT}: no .lean file of a default library at {tree.commit}")
    src_tree = (git(tree.repo, *OFFLINE_ARGS, "rev-parse", f"{tree.commit}:{prefix.rstrip('/')}",
                    env=offline_env()).decode().strip() if prefix else None)

    found: list[dict] = []
    in_libs = sorted(lib_of)
    for i in range(0, len(in_libs), READ_CHUNK):
        chunk = in_libs[i:i + READ_CHUNK]
        blobs = tree.read_many(chunk)
        for p in chunk:
            try:
                text = blobs[p].decode("utf-8")
            except UnicodeDecodeError:
                rejected.append({"kind": "file", "reason": "not_utf8", "candidate": p})
                continue
            for d in file_decls(p, prefix, text, tree.commit):
                d["lean_lib"] = lib_of[p]
                found.append(d)

    # One id per Lean name. A public name declared in two modules (possible when no module
    # imports both) is not one declaration, so neither is exported.
    by_name: dict[str, list[dict]] = {}
    for d in found:
        by_name.setdefault(d["name"], []).append(d)
    names, rows = [], []
    for name in sorted(by_name):
        ds = by_name[name]
        if len(ds) > 1:
            rejected.append({"kind": "decl", "reason": "ambiguous_name", "candidate": name,
                             "modules": sorted(d["module"] for d in ds)})
            continue
        d = ds[0]
        did = D.decl_id(LIBRARY, name)
        source = {"repo": REPO, "commit": tree.commit, "path": d["path"], "line": d["line"]}
        names.append({"id": did, "name": name, "module": d["module"], "lean_lib": d["lean_lib"],
                      "kind": d["kind"], "private": d["private"], "source": source})
        rows.append({"id": did, "name": name, "source_name": d["fq"], "module": d["module"],
                     "lean_lib": d["lean_lib"], "kind": d["kind"], "private": d["private"],
                     "line": d["line"], "signature": d["signature"], "doc": d["doc"], "refs": d["refs"],
                     "source": source})

    data = {"names.jsonl": D.jsonl_bytes(names), "decls.jsonl": D.jsonl_bytes(rows),
            "deps.jsonl": D.jsonl_bytes([]), "rejected.jsonl": D.jsonl_bytes(D.rejected_rows(rejected))}
    counts = {
        "files": len(paths),
        "files_in_default_libs": len(lib_of),
        "files_with_decls": len({d["path"] for d in found}),
        "names": len(names),
        "decls": len(rows),
        "decls_by_kind": D.count(rows, lambda r: str(r["kind"])),
        "decls_by_lean_lib": D.count(rows, lambda r: r["lean_lib"]),
        "decls_private": sum(1 for r in rows if r["private"]),
        "decls_with_doc": sum(1 for r in rows if r["doc"]),
        "deps_by_kind": {},
        "rejected_by_reason": D.count(rejected, lambda r: f"{r['kind']}/{r['reason']}"),
    }
    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "library": LIBRARY,
        "inputs": {
            INPUT: {"url": URL, "repo": REPO, "commit": tree.commit, "src_tree": src_tree,
                    "fetched_at": (pins.get(INPUT) or {}).get("fetched_at")},
        },
        # What lakefile.toml says at the commit: the libraries read and the ones left out.
        "lake": {"src_dir": prefix.rstrip("/") or ".", "default_libs": [n for n, _ in libs],
                 "other_libs": other_libs},
        "reader": {f: sha256_file(os.path.join(ROOT, f)) for f in READER_FILES},
        # Dependency edges need the elaborated project; the source text does not give them.
        "deps": "none: read from the source text, no Lean run",
        "counts": counts,
        "attribution": ATTRIBUTION,
    }
    return D.seal(out, data, manifest)
