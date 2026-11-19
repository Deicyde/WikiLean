"""Offline, deterministic build of the `wikilean.brain.plans/v1` export.

Reads only the pinned inputs under --inputs (see fetch.py for the layout) and writes the
export under --out. The same inputs give byte-identical output: no clock, no network, no
host data, every row sorted by id. Prose is never copied: titles and keys only, every
other fact is a pointer {repo, commit, path, line} into the pinned TauCetiRoadmap commit.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import tomllib
from collections import Counter, defaultdict

from . import parse
from .canon import ROADMAP_REPO, GitTree, dumps, jsonl_bytes, sha256_bytes, sha256_file

SCHEMA = "wikilean.brain.plans/v1"
PRODUCER = {"name": "scripts.tauceti_plans", "version": "1.0.0"}
ORIGINS = {"TauCetiRoadmap": "active", "Completed": "completed"}
STATES = {"done", "partial", "untouched"}
FILES = ["roadmaps.jsonl", "layers.jsonl", "edges.jsonl", "decl_refs.jsonl", "xrefs.jsonl",
         "rejected.jsonl"]
CLOCK_KEYS = {"fetched_at"}

URLS = {
    "TauCetiRoadmap": "https://github.com/TauCetiProject/TauCetiRoadmap",
    "trough": "https://github.com/Deicyde/trough",
    "atlas": "https://taucetiproject.org/atlas/",
    "mathlib_decls": "https://leanprover-community.github.io/mathlib4_docs/declarations/declaration-data.bmp",
    "tauceti_decls": "https://taucetiproject.github.io/TauCeti/docs/declarations/declaration-data.bmp",
}
ATTRIBUTION = [
    {"work": "TauCetiRoadmap", "url": URLS["TauCetiRoadmap"], "license": "Apache-2.0",
     "holder": "TauCeti contributors",
     "use": "roadmap and layer titles and keys, coverage states; everything else is a pointer"},
    {"work": "TauCeti", "url": "https://github.com/TauCetiProject/TauCeti", "license": "Apache-2.0",
     "holder": "TauCeti contributors", "use": "declaration names (doc-gen4 index) for resolution"},
    {"work": "tauceti-explorer (Tau Ceti atlas)", "url": "https://github.com/CBirkbeck/tauceti-explorer",
     "license": "Apache-2.0", "holder": "tauceti-explorer contributors",
     "use": "candidate dependency edges, each re-verified against the pinned roadmap commit"},
    {"work": "Mathlib", "url": "https://github.com/leanprover-community/mathlib4", "license": "Apache-2.0",
     "holder": "Mathlib contributors", "use": "declaration names (doc-gen4 index) for resolution"},
]


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def read_pins(inputs: str) -> dict:
    """Pins written by `fetch` (PINS.json), else run.sh's PINS.txt, else none."""
    for name in ("PINS.json", "PINS.txt"):
        if os.path.exists(os.path.join(inputs, name)):
            return read_pins_file(os.path.join(inputs, name))
    return {k: {} for k in URLS}


def read_pins_file(pt: str) -> dict:
    """A PINS.json, or run 1's PINS.txt (`<name> <commit>` and `<sha256>  <path>` lines).
    Returns {input: {commit?, sha256?, docs_commit?, fetched_at?}} keyed by the names in URLS."""
    with open(pt, encoding="utf-8") as f:
        head = f.read(1)
    if head == "{":
        with open(pt, encoding="utf-8") as f:
            return {k: {} for k in URLS} | json.load(f)["inputs"]
    pins: dict = {k: {} for k in URLS}
    names = {"atlas-data.json": "atlas", "mathlib.bmp": "mathlib_decls", "tauceti.bmp": "tauceti_decls"}
    fetched = None
    with open(pt, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[0] == "fetched_at":
                fetched = parts[1]
            elif parts[0] in ("TauCetiRoadmap", "trough"):
                pins[parts[0]]["commit"] = parts[1]
            elif re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                base = os.path.basename(parts[1])
                if base in names:
                    pins[names[base]]["sha256"] = parts[0]
    for k in pins:
        pins[k]["fetched_at"] = fetched
    return pins


def load_inputs(inputs: str) -> dict:
    pins = read_pins(inputs)
    rm = GitTree(os.path.join(inputs, "TauCetiRoadmap"))
    tr = GitTree(os.path.join(inputs, "trough"))
    for name, tree in (("TauCetiRoadmap", rm), ("trough", tr)):
        want = pins.get(name, {}).get("commit")
        if want and want != tree.commit:
            raise BuildError(f"{name}: HEAD {tree.commit} does not match pinned {want}")
    files = {"atlas": "atlas-data.json", "mathlib_decls": "decls/mathlib.bmp",
             "tauceti_decls": "decls/tauceti.bmp"}
    shas = {}
    for name, rel in files.items():
        sha = sha256_file(os.path.join(inputs, rel))
        want = pins.get(name, {}).get("sha256")
        if want and want != sha:
            raise BuildError(f"{rel}: sha256 {sha} does not match pinned {want}")
        shas[name] = sha
    trough_files = {}
    for rel in ("deps.json", "selection.json", "roadmaps.json", "source.json"):
        path = f"data/tauceti/{rel}"
        if not tr.exists(path):
            raise BuildError(f"trough: {path} missing at {tr.commit}")
        trough_files[rel] = tr.read(path)
    with open(os.path.join(inputs, files["atlas"]), encoding="utf-8") as f:
        atlas = json.load(f)

    def decl_index(rel):
        with open(os.path.join(inputs, rel), encoding="utf-8") as f:
            d = json.load(f)
        return set(d.get("declarations", {})), set(d.get("modules", {}))

    m_decls, m_mods = decl_index(files["mathlib_decls"])
    t_decls, t_mods = decl_index(files["tauceti_decls"])
    return {"pins": pins, "roadmap_tree": rm, "trough_tree": tr, "shas": shas,
            "trough_files": trough_files, "atlas": atlas,
            "mathlib": m_decls, "tauceti": t_decls, "modules": m_mods | t_mods}


# ---------------------------------------------------------------- roadmaps and layers

def pointer(commit: str, path: str, line: int | None = None) -> dict:
    p = {"repo": ROADMAP_REPO, "commit": commit, "path": path}
    if line is not None:
        p["line"] = line
    return p


def discover_roadmaps(tree: GitTree) -> list[str]:
    """Folders under TauCetiRoadmap/ or Completed/ (any depth) holding a README.md."""
    out = []
    for path in tree.blobs:
        parts = path.split("/")
        if parts[-1] == "README.md" and parts[0] in ORIGINS and len(parts) >= 3:
            out.append("/".join(parts[:-1]))
    return sorted(out)


def roadmap_title(lines: list[str], hs: list[dict], folder: str) -> str:
    for h in hs:
        if h["level"] == 1:
            return re.sub(r"^Roadmap\s*:\s*", "", h["title"]).strip() or folder.rsplit("/", 1)[1]
    return folder.rsplit("/", 1)[1]


def build_layers(rid: str, readme_path: str, hs: list[dict], commit: str) -> list[dict]:
    layers = []
    for h in hs:
        key = parse.layer_key(h["title"])
        if key is None or h["level"] == 1:
            continue
        layers.append({"id": f"{rid}#{h['anchor']}", "roadmap": rid, "ordinal": len(layers),
                       "key": key, "title": h["title"],
                       "anchor": h["anchor"], "level": h["level"], "line": h["line"],
                       "source": pointer(commit, readme_path, h["line"])})
    return layers


def section_owner(hs: list[dict], layers: list[dict], nlines: int) -> list[str | None]:
    """For each README line (index 0 = line 1), the innermost layer whose section holds it.
    A layer's section runs from its heading to the next heading of the same or a higher level."""
    by_line = {l["line"]: l for l in layers}
    owner: list[str | None] = [None] * (nlines + 1)
    stack: list[tuple[int, str | None]] = []
    hi = 0
    for n in range(1, nlines + 1):
        if hi < len(hs) and hs[hi]["line"] == n:
            h = hs[hi]
            hi += 1
            while stack and stack[-1][0] >= h["level"]:
                stack.pop()
            lay = by_line.get(n)
            stack.append((h["level"], lay["id"] if lay else None))
        cur = None
        for _lvl, lid in reversed(stack):
            if lid:
                cur = lid
                break
        owner[n] = cur
    return owner


# ---------------------------------------------------------------- coverage

_COVERAGE = re.compile(r"^\s*<!--tauceti-coverage:v1 (.*)-->\s*$")


def apply_coverage(tree, texts, roadmaps, layers_by_rm, readme_sha, rejected):
    commit = tree.commit
    comments = defaultdict(list)  # roadmap id -> [(status path, line, payload)]
    for path in sorted(p for p in texts if p.endswith("/STATUS.md")):
        top = path.split("/")[0]
        for n, ln in enumerate(parse.lines_of(texts[path].decode("utf-8")), 1):
            if "tauceti-coverage" not in ln:
                continue
            m = _COVERAGE.match(ln)
            ev = pointer(commit, path, n)
            try:
                payload = json.loads(m.group(1)) if m else None
                rel = payload["roadmap"]
                layers = payload["layers"]
                if not isinstance(rel, str) or not isinstance(layers, list):
                    raise TypeError
            except (ValueError, KeyError, TypeError, AttributeError):
                rejected.append({"kind": "coverage", "reason": "coverage_unparseable", "evidence": ev})
                continue
            rid = f"tauceti:{top}/{rel}"
            if rid not in roadmaps:
                rejected.append({"kind": "coverage", "reason": "coverage_roadmap_not_found",
                                 "roadmap": rid, "evidence": ev})
                continue
            comments[rid].append((path, n, payload))
    for rid in sorted(comments):
        found = comments[rid]
        if len(found) > 1:
            for path, n, _ in found:
                rejected.append({"kind": "coverage", "reason": "coverage_duplicate_comment",
                                 "roadmap": rid, "evidence": pointer(commit, path, n)})
            continue
        path, n, payload = found[0]
        ev = pointer(commit, path, n)
        layers = layers_by_rm.get(rid, [])
        key_count = Counter(l["key"] for l in layers)
        ids = Counter(e.get("id") for e in payload["layers"] if isinstance(e, dict))
        current = payload.get("readme_sha") == readme_sha[rid] if payload.get("readme_sha") else None
        for e in payload["layers"]:
            lid = e.get("id") if isinstance(e, dict) else None
            state = e.get("state") if isinstance(e, dict) else None
            base = {"kind": "coverage", "roadmap": rid, "layer_key": lid if isinstance(lid, str) else None,
                    "evidence": ev}
            if not isinstance(lid, str) or state not in STATES:
                rejected.append({**base, "reason": "coverage_bad_entry"})
            elif ids[lid] > 1:
                rejected.append({**base, "reason": "coverage_duplicate_entry"})
            elif key_count[lid] == 0:
                rejected.append({**base, "reason": "coverage_no_matching_layer"})
            elif key_count[lid] > 1:
                rejected.append({**base, "reason": "coverage_ambiguous_layer_key"})
            else:
                lay = next(l for l in layers if l["key"] == lid)
                lay["state"] = state
                lay["status_source"] = ev
                lay["status_readme_current"] = current
                lay["status_tauceti_rev"] = payload.get("to_sha") if isinstance(payload.get("to_sha"), str) else None


# ---------------------------------------------------------------- edges

_MD_LINK_TARGET = re.compile(r"\]\(([^)\s]+)")


def _names_folder(line: str, other_folder: str, readme_path: str, shared_basenames: set[str]) -> bool:
    """Does the line name the roadmap folder `other_folder` (e.g. `Completed/Gamma`)?
    Its last component, with no letter or digit either side, is enough unless two roadmaps
    share that component; then only the full folder path or a link that resolves into it counts."""
    base = other_folder.rsplit("/", 1)[1]
    if base not in shared_basenames:
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(base)}(?![A-Za-z0-9])", line) is not None
    if re.search(rf"(?<![A-Za-z0-9/]){re.escape(other_folder)}(?![A-Za-z0-9])", line):
        return True
    here = posixpath.dirname(readme_path)
    for m in _MD_LINK_TARGET.finditer(line):
        target = m.group(1).split("#", 1)[0]
        if not target or "://" in target:
            continue
        resolved = posixpath.normpath(posixpath.join(here, target))
        if resolved == other_folder or resolved.startswith(other_folder + "/"):
            return True
    return False


def _atlas_path(source_path: str) -> str | None:
    if source_path.startswith("content/tau-ceti/"):
        rest = source_path[len("content/tau-ceti/"):]
        return rest if rest.startswith("Completed/") else "TauCetiRoadmap/" + rest
    m = re.match(r"^https://github\.com/TauCetiProject/TauCetiRoadmap/blob/[^/]+/(.+)$", source_path)
    if m:
        return m.group(1)
    if source_path.split("/")[0] in ORIGINS:
        return source_path
    return None


def _owner_of_path(path: str, roadmaps: dict) -> str | None:
    folder = path.rsplit("/", 1)[0]
    rid = f"tauceti:{folder}"
    return rid if rid in roadmaps else None


def _evidence_candidates(edge: dict) -> list[dict] | str:
    """Normalise the atlas's evidence shapes to [{path, line, match?, span}] or a reason."""
    ev = edge.get("evidence")
    if not ev:
        return "no_evidence"
    items = ev if isinstance(ev, list) else [ev]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if "readme" in it and "line" in it:
            out.append({"src": it["readme"], "line": it["line"], "match": None, "span": 1})
        elif "sourcePath" in it and "sourceLine" in it:
            span = (it.get("text") or "").count("\n") + 1
            out.append({"src": it["sourcePath"], "line": it["sourceLine"], "match": it.get("match"),
                        "span": span})
    return out or "no_evidence_line"


def _verify_at(lines, c, path, other_folder, shared) -> int | str:
    """The line (1-based) of `lines` that verifies evidence item `c`, or the reason it fails."""
    line = c["line"]
    if not 1 <= line <= len(lines):
        return "evidence_line_missing"
    span = list(range(line, min(len(lines), line + c["span"] - 1) + 1))
    if c["match"]:
        span = [n for n in span if c["match"] in lines[n - 1]]
        if not span:
            return "match_not_at_line"
    hit = next((n for n in span if _names_folder(lines[n - 1], other_folder, path, shared)), None)
    return "line_does_not_name_target" if hit is None else hit


def _check_evidence(c, texts, roadmaps, rs, rt, commit, lines_cache, shared, embedded) -> dict | str:
    """A verified pointer for one atlas evidence item, or the reason it fails. The line must
    sit in one endpoint's README, hold the atlas's `match` text when it gives one, and name the
    other endpoint's roadmap folder: first at the pinned TauCetiRoadmap commit, else at that
    line of the README copy the atlas embeds under the evidence's own sourcePath (the atlas
    took its line numbers from its own snapshot). Nothing is searched or relocated."""
    path = _atlas_path(c["src"]) if isinstance(c["src"], str) else None
    line = c["line"] if isinstance(c["line"], int) and not isinstance(c["line"], bool) else None
    if path is None or line is None:
        return "unrecognized_evidence_path"
    owner = _owner_of_path(path, roadmaps)
    if owner not in (rs, rt):
        return "evidence_not_in_endpoint_roadmap"
    other = rt if owner == rs else rs
    other_folder = other.split(":", 1)[1]
    if path in texts:
        if path not in lines_cache:
            lines_cache[path] = parse.lines_of(texts[path].decode("utf-8"))
        why = _verify_at(lines_cache[path], c, path, other_folder, shared)
        if not isinstance(why, str):
            return {**pointer(commit, path, why), "evidence_input": "tauceti_roadmap", "evidence_public": True}
    else:
        why = "evidence_file_missing"
    copy = embedded["lines"].get(c["src"])
    if copy is not None:
        hit = _verify_at(copy, c, path, other_folder, shared)
        if not isinstance(hit, str):
            # Not followable to a public commit: never present this as a public citation.
            return {"evidence_input": "atlas_embedded", "evidence_public": False,
                    "atlas_sha256": embedded["sha256"], "source_path": c["src"], "path": path, "line": hit}
    return why


def verify_edges(atlas, tree, texts, roadmaps, atlas_to_layer, layer_ids, rejected, atlas_sha256=None):
    commit = tree.commit
    # The README copies the atlas publishes, keyed by their own sourcePath.
    embedded = {"sha256": atlas_sha256, "lines": {
        r["sourcePath"]: parse.lines_of(r["readme"]) for r in atlas.get("roadmaps", [])
        if isinstance(r, dict) and isinstance(r.get("sourcePath"), str)
        and isinstance(r.get("readme"), str) and r["readme"]}}
    lines_cache: dict[str, list[str]] = {}
    base_count = Counter(rid.rsplit("/", 1)[1] for rid in roadmaps)
    shared = {b for b, n in base_count.items() if n > 1}
    kept: dict[tuple[str, str], dict] = {}
    candidates = [(e, e.get("kind") or "edge") for e in atlas.get("edges", [])]
    candidates += [(e, e.get("kind") or "stage_edge") for e in atlas.get("stageEdges", [])]
    for edge, akind in candidates:
        # The atlas writes prerequisite -> consumer; rejected rows keep its ids as atlas_*.
        src, tgt = edge.get("source"), edge.get("target")
        base = {"kind": "edge", "atlas_kind": akind, "atlas_source": src, "atlas_target": tgt}

        def endpoint(x):
            if not isinstance(x, str):
                return None
            if "#" in x:
                return x if x in layer_ids else atlas_to_layer.get(x)
            return x if x in roadmaps else None

        s, t = endpoint(src), endpoint(tgt)
        if s is None or t is None:
            rejected.append({**base, "reason": "endpoint_not_at_pin"})
            continue
        if s == t:
            rejected.append({**base, "reason": "self_edge"})
            continue
        cands = _evidence_candidates(edge)
        if isinstance(cands, str):
            rejected.append({**base, "reason": cands})
            continue
        rs, rt = s.split("#")[0], t.split("#")[0]
        good, failed = [], []
        for c in cands:
            result = _check_evidence(c, texts, roadmaps, rs, rt, commit, lines_cache, shared, embedded)
            (failed if isinstance(result, str) else good).append((c, result))
        reasons = sorted({why for _c, why in failed})
        if good:
            for c, why in failed:
                path = _atlas_path(c["src"]) if isinstance(c["src"], str) else None
                ev = pointer(commit, path, c["line"]) if path and isinstance(c["line"], int) else None
                rejected.append({**base, "kind": "edge_evidence", "reason": why, "source": t, "target": s,
                                 "evidence": ev})
        good = [ptr for _c, ptr in good]
        if not good:
            rejected.append({**base, "reason": reasons[0] if len(reasons) == 1 else "no_evidence_verified",
                             "evidence_reasons": reasons})
            continue
        # brain/SCHEMA.md's direction: source is the consumer (atlas target), target the prerequisite.
        key = (t, s)
        row = kept.setdefault(key, {"id": f"depends:{t}->{s}", "kind": "depends", "source": t,
                                    "target": s, "via": "atlas", "atlas_kinds": [], "_ev": []})
        row["atlas_kinds"] = sorted(set(row["atlas_kinds"]) | {akind})
        row["_ev"].extend(good)
    rows = []
    for row in kept.values():
        # Public pointers first, so the row's evidence is public whenever any public pointer exists.
        evs = sorted({dumps(e): e for e in row.pop("_ev")}.values(),
                     key=lambda e: (not e["evidence_public"], e["path"], e["line"], e["evidence_input"]))
        row["evidence"] = evs[0]
        row["more_evidence"] = evs[1:]
        row["evidence_public"] = evs[0]["evidence_public"]
        rows.append(row)
    return rows


# ---------------------------------------------------------------- decl refs and xrefs

MAX_CANDIDATES = 8


def with_parents(scopes) -> set[str]:
    """Each namespace with every parent prefix of it: `A.B` gives `A.B` and `A`."""
    out = set()
    for n in scopes:
        parts = n.split(".")
        out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


class Resolver:
    """Resolves a written name against both `.bmp` indexes, built once per build.

    Order: exact entry; else exactly one `N.name` for a declared scope N (`via_namespace`);
    else, for a dotted name whose first segment is neither an entry's namespace component nor
    a namespace the roadmaps declare (`declared`), the rejection `local_term`; else
    `unqualified` when the name is an entry with whole leading components removed; else
    `planned`. A name under a declared namespace that no library has yet is a planned
    declaration in a planned namespace, so it stays `planned`."""

    def __init__(self, mathlib: set[str], tauceti: set[str], declared: set[str] = frozenset()):
        self.mathlib, self.tauceti = mathlib, tauceti
        self.declared = frozenset(declared)
        self.components: set[str] = set()
        self.by_last: dict[str, list[str]] = defaultdict(list)
        for name in sorted(mathlib | tauceti):
            parts = name.split(".")
            self.components.update(parts[:-1])
            if len(parts) > 1:
                self.by_last[parts[-1]].append(name)

    def library(self, name: str) -> str | None:
        return "mathlib" if name in self.mathlib else "tauceti" if name in self.tauceti else None

    def libraries(self, name: str) -> list[str]:
        return [lib for lib, s in (("mathlib", self.mathlib), ("tauceti", self.tauceti)) if name in s]

    def resolve(self, name: str, scopes: set[str] = frozenset(), exact_only: bool = False) -> dict:
        lib = self.library(name)
        if lib:
            return {"resolution": lib}
        if exact_only:
            return {"resolution": "planned"}
        hits = {}
        for ns in sorted(scopes):
            full = f"{ns}.{name}"
            if self.library(full):
                hits.setdefault(full, ns)
        if len(hits) == 1:
            full, ns = next(iter(hits.items()))
            return {"resolution": self.library(full), "via_namespace": ns, "resolved_name": full}
        parts = name.split(".")
        if (not hits and len(parts) > 1 and parts[0] not in self.components
                and parts[0] not in self.declared):
            return {"resolution": "local_term"}
        found = sorted(set(hits) | {f for f in self.by_last.get(parts[-1], ())
                                    if f.endswith("." + name)})
        if found:
            libs = sorted({lib for f in found for lib in self.libraries(f)})
            return {"resolution": "unqualified", "candidates": found[:MAX_CANDIDATES],
                    "candidates_total": len(found), "candidate_libraries": libs}
        return {"resolution": "planned"}


def resolve(name: str, ctx: dict, scopes: set[str] = frozenset(), exact_only: bool = False
            ) -> tuple[dict, list | None]:
    r = ctx["resolver"].resolve(name, scopes, exact_only)
    res = r["resolution"]
    if res == "mathlib":
        return r, ["mathlib_decls"]
    if res == "tauceti":
        return {**r, **stated(r.get("resolved_name", name), ctx)}, ["tauceti_decls"]
    return r, None


def stated(full: str, ctx: dict) -> dict:
    """Whether a Tau Ceti declaration is only stated (its proof is `sorry`), read from
    trough's deps.json, and only when trough analysed the commit the Tau Ceti docs show."""
    if ctx["stated_reason"]:
        return {"stated": None, "stated_reason": ctx["stated_reason"]}
    sorry = ctx["sorry"].get(full)
    if not isinstance(sorry, bool):
        return {"stated": None, "stated_reason": "not_in_deps"}
    return {"stated": sorry}


class Collector:
    """Deduplicates (owner, item) and keeps the first evidence by (path, line)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def add(self, rid, lid, item_key, fields, ev):
        owner = lid or rid
        rid_ = f"{owner}::{item_key}"
        row = self.rows.get(rid_)
        if row is None:
            self.rows[rid_] = {"id": rid_, "roadmap": rid, "layer": lid, **fields, "evidence": ev,
                               "occurrences": 1}
        else:
            row["occurrences"] += 1
            if (ev["path"], ev["line"]) < (row["evidence"]["path"], row["evidence"]["line"]):
                row["evidence"] = ev


def scan_text_refs(rid, path, lines, owner_of, ctx, decls, xrefs, rejected, *, fenced=None):
    commit = ctx["commit"]
    for n, ln in enumerate(lines, 1):
        lid = owner_of(n)
        ev = pointer(commit, path, n)
        if fenced is None or not fenced[n - 1]:
            if path.endswith("README.md"):
                for name, why in parse.backticked_names(ln):
                    add_decl(rid, lid, name, ev, ctx, decls, rejected, why)
        for tag, why in parse.stacks_tags(ln):
            if why:
                rejected.append({"kind": "xref", "reason": why, "candidate": tag, "roadmap": rid,
                                 "layer": lid, "evidence": ev})
            else:
                xrefs.add(rid, lid, f"xref:stacks:{tag}", {"xref": f"xref:stacks:{tag}", "system": "stacks"}, ev)
        for aid, why in parse.arxiv_ids(ln):
            if why:
                rejected.append({"kind": "xref", "reason": why, "candidate": aid, "roadmap": rid,
                                 "layer": lid, "evidence": ev})
            else:
                xrefs.add(rid, lid, f"lit:{aid}", {"xref": f"lit:{aid}", "system": "arxiv"}, ev)


def add_decl(rid, lid, name, ev, ctx, decls, rejected, why=None, *, declared=False):
    """`declared`: the name is a declaration Suggested.lean makes (its full name), so only an
    exact entry resolves it; scope and suffix resolution apply to names a README writes."""
    r, by = resolve(name, ctx, ctx["scopes"].get(rid, frozenset()), exact_only=declared)
    res = r["resolution"]
    if why is None and res in ("planned", "local_term", "unqualified"):
        if name in ctx["modules"]:
            why = "module_name"
        elif name.split(".")[0] == "Mathlib":
            why = "module_path"
        elif name in ctx["roadmap_modules"]:
            why = "roadmap_module"
        elif res == "local_term":
            why = "local_term"
    if why:
        rejected.append({"kind": "decl_ref", "reason": why, "candidate": name, "roadmap": rid,
                         "layer": lid, "evidence": ev})
        return
    decls.add(rid, lid, name, {"name": name, **r, "resolved_by": by}, ev)


# ---------------------------------------------------------------- main build

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


def build(inputs: str, out: str) -> dict:
    # The export is never committed: it is far over GitHub's 100 MB file limit.
    tree_root = git_work_tree_holding(out)
    if tree_root is not None:
        raise BuildError(f"--out {out} lies inside the git work tree {tree_root}; write the export elsewhere")
    inp = load_inputs(inputs)
    tree: GitTree = inp["roadmap_tree"]
    commit = tree.commit
    folders = discover_roadmaps(tree)
    wanted = []
    for fo in folders:
        wanted += [f"{fo}/README.md", f"{fo}/STATUS.md", f"{fo}/Suggested.lean", f"{fo}/metadata.toml"]
    texts = tree.read_many(wanted)
    rejected: list[dict] = []

    roadmaps, layers_by_rm, readme_sha, parsed = {}, {}, {}, {}
    for fo in folders:
        rid = f"tauceti:{fo}"
        readme_path = f"{fo}/README.md"
        raw = texts[readme_path]
        readme_sha[rid] = sha256_bytes(raw)
        lines = parse.lines_of(raw.decode("utf-8"))
        hs = parse.headings(lines)
        topic = None
        meta_raw = texts.get(f"{fo}/metadata.toml")
        if meta_raw is not None:
            try:
                t = tomllib.loads(meta_raw.decode("utf-8")).get("topic")
                topic = t if isinstance(t, str) else None
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                rejected.append({"kind": "roadmap", "reason": "metadata_unparseable", "roadmap": rid,
                                 "evidence": pointer(commit, f"{fo}/metadata.toml")})
        parent = "/".join(fo.split("/")[:-1])
        roadmaps[rid] = {"id": rid, "title": roadmap_title(lines, hs, fo), "origin": ORIGINS[fo.split("/")[0]],
                         "topic": topic, "parent": f"tauceti:{parent}" if f"tauceti:{parent}" in
                         {f"tauceti:{x}" for x in folders} else None,
                         "source": pointer(commit, readme_path)}
        lays = build_layers(rid, readme_path, hs, commit)
        for l in lays:
            l.update({"state": "unassessed", "status_source": None, "status_readme_current": None,
                      "status_tauceti_rev": None})
        layers_by_rm[rid] = lays
        parsed[rid] = (readme_path, lines, hs)

    apply_coverage(tree, texts, roadmaps, layers_by_rm, readme_sha, rejected)

    # Atlas stage ids -> our layer ids: same anchor, else the same heading title (unique).
    layer_ids = {l["id"] for ls in layers_by_rm.values() for l in ls}
    atlas_to_layer, atlas_ids_for = {}, defaultdict(list)
    for st in inp["atlas"].get("stages", []):
        sid, owner = st.get("id"), st.get("owner")
        if not isinstance(sid, str) or owner not in layers_by_rm:
            continue
        if sid in layer_ids:
            atlas_to_layer[sid] = sid
            continue
        same = [l for l in layers_by_rm[owner] if l["title"] == st.get("title")]
        if len(same) != 1:
            same = [l for l in layers_by_rm[owner] if l["key"] == st.get("key")
                    and l["line"] == st.get("sourceLine")
                    and l["source"]["path"] == st.get("repositoryPath")]
        if len(same) == 1:
            atlas_to_layer[sid] = same[0]["id"]
            atlas_ids_for[same[0]["id"]].append(sid)
    for ls in layers_by_rm.values():
        for l in ls:
            al = sorted(atlas_ids_for.get(l["id"], []))
            l["atlas_id"] = al[0] if len(al) == 1 else None

    edges = verify_edges(inp["atlas"], tree, texts, roadmaps, atlas_to_layer, layer_ids, rejected,
                         inp["shas"]["atlas"])

    deps = json.loads(inp["trough_files"]["deps.json"])
    sorry = {k: v.get("sorry") for k, v in deps.get("declarations", {}).items() if isinstance(v, dict)}
    # The roadmap repo's own Lean modules (`TauCetiRoadmap.Foo`, `TauCetiRoadmap.Foo.Suggested`).
    roadmap_modules = set()
    for fo in folders:
        if fo.startswith("TauCetiRoadmap/"):
            mod = fo.replace("/", ".")
            roadmap_modules |= {mod, mod + ".Suggested"}
    # Each roadmap's declared scopes: `namespace`/`open` in Suggested.lean and README code blocks.
    # Namespace components the roadmaps declare, across all roadmaps: every component of a
    # declared scope, and every non-final component of a name Suggested.lean declares. These
    # keep `local_term` from rejecting planned names in planned namespaces.
    scopes = {}
    declared_ns: set[str] = set()
    for rid in sorted(parsed):
        readme_path, lines, _hs = parsed[rid]
        mask = parse.fence_mask(lines)
        found = parse.code_scopes([ln for ln, f in zip(lines, mask) if f])
        sug_path = readme_path[:-len("README.md")] + "Suggested.lean"
        if sug_path in texts:
            sug_decls, _markers = parse.lean_scan(texts[sug_path].decode("utf-8"), found)
            for d in sug_decls:
                declared_ns.update(d["name"].split(".")[:-1])
        scopes[rid] = with_parents(found)
        for s in scopes[rid]:
            declared_ns.update(s.split("."))
    ctx = {"roadmap_modules": roadmap_modules, "commit": commit, "modules": inp["modules"],
           "resolver": Resolver(inp["mathlib"], inp["tauceti"], declared_ns), "scopes": scopes,
           "sorry": sorry, "stated_reason": stated_reason(inp)}
    decls, xrefs = Collector(), Collector()
    for rid in sorted(parsed):
        readme_path, lines, hs = parsed[rid]
        owners = section_owner(hs, layers_by_rm[rid], len(lines))
        scan_text_refs(rid, readme_path, lines, lambda n: owners[n], ctx, decls, xrefs, rejected,
                       fenced=parse.fence_mask(lines))
        sug_path = readme_path[:-len("README.md")] + "Suggested.lean"
        if sug_path in texts:
            text = texts[sug_path].decode("utf-8")
            sl = parse.lines_of(text)
            found, markers = parse.lean_scan(text)
            keys = Counter(l["key"] for l in layers_by_rm[rid])
            marker_layer = []
            for mk in markers:
                key = parse.layer_key(mk["title"])
                lay = None
                if key and keys[key] == 1:
                    lay = next(l["id"] for l in layers_by_rm[rid] if l["key"] == key)
                elif key:
                    rejected.append({"kind": "lean_marker", "reason": "marker_layer_not_unique"
                                     if keys[key] else "marker_layer_not_found", "candidate": key,
                                     "roadmap": rid, "evidence": pointer(commit, sug_path, mk["line"])})
                marker_layer.append((mk["line"], lay))

            def lean_owner(n, ml=marker_layer):
                cur = None
                for ln_, lay in ml:
                    if ln_ > n:
                        break
                    cur = lay
                return cur

            for d in found:
                add_decl(rid, lean_owner(d["line"]), d["name"], pointer(commit, sug_path, d["line"]),
                         ctx, decls, rejected, declared=True)
            scan_text_refs(rid, sug_path, sl, lean_owner, ctx, decls, xrefs, rejected)

    layer_rows = []
    for ls in layers_by_rm.values():
        for l in ls:
            layer_rows.append({k: l[k] for k in ("id", "roadmap", "ordinal", "key", "title", "state", "status_source",
                                                 "status_readme_current", "status_tauceti_rev",
                                                 "atlas_id", "source")})
    rej_rows = {}
    for r in rejected:
        rid_ = f"{r['kind']}:{sha256_bytes(dumps(r).encode())[:20]}"
        if rid_ in rej_rows:
            rej_rows[rid_]["count"] += 1
        else:
            rej_rows[rid_] = {"id": rid_, **r, "count": 1}

    data = {
        "roadmaps.jsonl": jsonl_bytes(list(roadmaps.values())),
        "layers.jsonl": jsonl_bytes(layer_rows),
        "edges.jsonl": jsonl_bytes(edges),
        "decl_refs.jsonl": jsonl_bytes(list(decls.rows.values())),
        "xrefs.jsonl": jsonl_bytes(list(xrefs.rows.values())),
        "rejected.jsonl": jsonl_bytes(list(rej_rows.values())),
    }
    counts = {
        "layers_by_state": dict(sorted(Counter(r["state"] for r in layer_rows).items())),
        "decl_refs_by_resolution": dict(sorted(Counter(r["resolution"] for r in decls.rows.values()).items())),
        "tauceti_decl_refs_by_stated": dict(sorted(Counter(
            str(r["stated"]).lower() if r["stated"] is not None else f"null/{r['stated_reason']}"
            for r in decls.rows.values() if r["resolution"] == "tauceti").items())),
        "xrefs_by_system": dict(sorted(Counter(r["system"] for r in xrefs.rows.values()).items())),
        "rejected_by_reason": dict(sorted(Counter(f"{r['kind']}/{r['reason']}" for r in rejected).items())),
    }
    manifest = make_manifest(inp, data, counts)
    os.makedirs(out, exist_ok=True)
    for name, blob in data.items():
        _write(os.path.join(out, name), blob)
    _write(os.path.join(out, "manifest.json"), (json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                                                           indent=1) + "\n").encode("utf-8"))
    return manifest


def _write(path: str, blob: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


def strip_clocks(obj):
    if isinstance(obj, dict):
        return {k: strip_clocks(v) for k, v in obj.items() if k not in CLOCK_KEYS}
    if isinstance(obj, list):
        return [strip_clocks(v) for v in obj]
    return obj


def logical_root(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "logical_root"}
    return sha256_bytes(dumps(strip_clocks(body)).encode("utf-8"))


def tauceti_docs_commit(inp: dict) -> str | None:
    c = (inp["pins"].get("tauceti_decls") or {}).get("docs_commit")
    return c if isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}", c) else None


def stated_reason(inp: dict) -> str | None:
    """None when trough's deps.json describes the Tau Ceti commit the docs were built from."""
    docs = tauceti_docs_commit(inp)
    if docs is None:
        return "docs_commit_unknown"
    rev = json.loads(inp["trough_files"]["source.json"]).get("rev")
    return None if rev == docs else "commit_mismatch"


def make_manifest(inp: dict, data: dict, counts: dict) -> dict:
    pins, shas = inp["pins"], inp["shas"]
    atlas = inp["atlas"]
    source = json.loads(inp["trough_files"]["source.json"])
    snap = (atlas.get("meta") or {}).get("repositorySnapshot") or {}

    def fetched(name):
        return (pins.get(name) or {}).get("fetched_at")

    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "inputs": {
            "TauCetiRoadmap": {"url": URLS["TauCetiRoadmap"], "repo": ROADMAP_REPO,
                               "commit": inp["roadmap_tree"].commit, "fetched_at": fetched("TauCetiRoadmap")},
            "trough": {"url": URLS["trough"], "repo": "Deicyde/trough", "commit": inp["trough_tree"].commit,
                       "fetched_at": fetched("trough"),
                       "tauceti_rev": source.get("rev"), "tauceti_repo": source.get("repo"),
                       "files": {f"data/tauceti/{k}": sha256_bytes(v)
                                 for k, v in sorted(inp["trough_files"].items())}},
            "atlas": {"url": URLS["atlas"], "path": "atlas-data.json", "sha256": shas["atlas"],
                      "exported_at": (atlas.get("taucetiProgress") or {}).get("exportedAt"),
                      "edition": (atlas.get("meta") or {}).get("edition"),
                      "roadmap_snapshot_head": snap.get("head"),
                      # The commit the atlas read its READMEs at: a local working tree, on no
                      # public remote, so it cannot be pinned; edges verify at our pin or
                      # against the README copies embedded in this file instead.
                      "atlas_snapshot_commit": {"commit": snap.get("head"), "source": snap.get("source"),
                                                "publicly_available": False},
                      "fetched_at": fetched("atlas")},
            "mathlib_decls": {"url": URLS["mathlib_decls"], "path": "decls/mathlib.bmp",
                              "sha256": shas["mathlib_decls"],
                              "docs_commit": (pins.get("mathlib_decls") or {}).get("docs_commit"),
                              "fetched_at": fetched("mathlib_decls")},
            "tauceti_decls": {"url": URLS["tauceti_decls"], "path": "decls/tauceti.bmp",
                              "sha256": shas["tauceti_decls"], "docs_commit": tauceti_docs_commit(inp),
                              "fetched_at": fetched("tauceti_decls")},
        },
        # What a decl_refs row's `resolved_by` names, keyed by input name.
        "resolved_by": {
            "mathlib_decls": {"path": "decls/mathlib.bmp", "sha256": shas["mathlib_decls"],
                              "pin": {"sha256": shas["mathlib_decls"]}},
            "tauceti_decls": {"path": "decls/tauceti.bmp", "sha256": shas["tauceti_decls"],
                              "pin": {"sha256": shas["tauceti_decls"]}},
            "trough_deps": {"path": "trough/data/tauceti/deps.json",
                            "sha256": sha256_bytes(inp["trough_files"]["deps.json"]),
                            "pin": {"repo": "Deicyde/trough", "commit": inp["trough_tree"].commit}},
        },
        "files": {name: {"rows": blob.count(b"\n"), "sha256": sha256_bytes(blob), "bytes": len(blob)}
                  for name, blob in sorted(data.items())},
        "counts": counts,
        "clock_fields": sorted(CLOCK_KEYS),
        "attribution": ATTRIBUTION,
    }
    manifest["logical_root"] = logical_root(manifest)
    return manifest
