"""Offline, deterministic build of the `wikilean.brain.rh-decls/v1` export.

Reads leanprover-community/riemann-hypothesis at the pinned commit (`rh/` under --inputs,
written by this producer's fetch): its `lakefile.toml`, then every module of the Lake
libraries `lake build` builds (`defaultTargets`). Writes names.jsonl, decls.jsonl, deps.jsonl,
rejected.jsonl and manifest.json under --out. Same inputs and the same reader, same bytes.
Contract: docs/BRAIN-PRODUCERS.md, producer 4.

The repository publishes no doc-gen4 index and trough has no record of it, so the
declarations come from its source text, read with the Brain's own Lean reader: its comment
stripping and declaration pattern (`build_common`), and the frontier harvest's docstring,
kind and statement snippets (`formal_conjectures`). It skips what the source does not spell
out (structure fields, anonymous instances, generated names). No Lean runs, so deps.jsonl is
empty.

The scopes are this producer's own pass (`scan`), for two things the shared reader's
`_lean_decl_lines` does not do. Every RH file is a `module`, and in a module a declaration
is private unless it is marked `public` or sits in a `public section`; Lean names it
`_private.<module>.0.<name>`. And the shared reader only knows a bare `section`, so there the
`end` of a `public section` or `noncomputable section` closes the enclosing namespace instead.
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
import common  # noqa: E402  (brain/ingest/common.py: clean_snippet)
import formal_conjectures as fc  # noqa: E402  (brain/ingest/formal_conjectures.py: decl context)

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
READER_FILES = ("brain/build_common.py", "brain/ingest/common.py", "brain/ingest/formal_conjectures.py")
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

_IDENT = build_common._LEAN_IDENT
_DECL = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)*((?:{build_common._LEAN_MOD}\s+)*){build_common._LEAN_KW}\s+"
                   rf"(_root_\.)?({_IDENT})")
_MODULE = re.compile(r"^\s*module\s*$")
_NAMESPACE = re.compile(rf"^\s*namespace\s+({_IDENT})\s*$")
# `section`, `noncomputable section`, `@[expose] public section`, `public section Name`, ...
_SECTION = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)*((?:(?:public|noncomputable|meta)\s+)*)section"
                      rf"(?:\s+({_IDENT}))?\s*$")
_MUTUAL = re.compile(r"^\s*mutual\s*$")
_END = re.compile(rf"^\s*end(?:\s+({_IDENT}))?\s*$")


def scan(lines: list[str]) -> list[tuple[int, str, bool]]:
    """(line index, full name as written, private) for each declaration the source spells out,
    in file order; the first of a repeated name wins, as in `_lean_decl_lines`.

    Scopes are `namespace`, every form of `section`, and `mutual`, each closed by `end`. In a
    file whose first command is `module`, a scope is public when it is a `public ... section`
    or sits in one, and a declaration is private unless it is marked `public` or is in a
    public scope (checked against Lean 4.37). Elsewhere only `private` makes it private."""
    stack: list[tuple[str, str | None, bool]] = []  # (kind, name, public scope)
    is_module = None
    seen: set[str] = set()
    out = []
    for i, code in build_common._strip_lean_comments(lines):
        if not code.strip():
            continue
        if is_module is None:
            is_module = bool(_MODULE.match(code))
            if is_module:
                continue
        public = bool(stack) and stack[-1][2]
        if m := _NAMESPACE.match(code):
            stack.append(("ns", m.group(1), public))
        elif m := _SECTION.match(code):
            stack.append(("sec", m.group(2), public or "public" in m.group(1).split()))
        elif _MUTUAL.match(code):
            stack.append(("mutual", None, public))
        elif m := _END.match(code):
            name = m.group(1)
            if not name:
                if stack:
                    stack.pop()
            else:  # `end A.B` closes `namespace A.B`, or `namespace A` with `namespace B` in it
                for j in range(len(stack) - 1, -1, -1):
                    if ".".join(n for _, n, _ in stack[j:] if n) == name:
                        del stack[j:]
                        break
        elif d := _DECL.match(code):
            mods, root, name = d.group(1).split(), d.group(2), d.group(3).rstrip(".")
            prefix = ".".join(n for k, n, _ in stack if k == "ns" and n)
            fq = name if root or not prefix else f"{prefix}.{name}"
            if not name or fq in seen:
                continue
            seen.add(fq)
            out.append((i, fq, "private" in mods or (is_module and "public" not in mods and not public)))
    return out


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


def file_decls(path: str, prefix: str, text: str) -> list[dict]:
    """Every declaration `scan` finds in one source file, with the frontier harvest's kind,
    statement snippet, docstring and links (as `lean_repo.harvest_rows` makes them)."""
    module = path[len(prefix):-len(".lean")].replace("/", ".")
    lines = text.splitlines()
    out = []
    for idx, fq, private in scan(lines):
        _, doc = fc._decl_context(lines, idx)
        kw = fc._KW.search(re.sub(r"@\[[^\]]*\]", "", lines[idx]))
        out.append({"fq": fq, "name": lean_name(fq, module, private), "module": module,
                    "kind": kw.group(1) if kw else None, "private": private, "line": idx + 1,
                    "signature": fc._code_snippet(lines, idx) or None,
                    "doc": common.clean_snippet(doc, fc.DOCSTRING_MAX) or None,
                    "refs": fc._refs(doc) or None, "path": path})
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
            for d in file_decls(p, prefix, text):
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
