"""Offline, deterministic conversion of a sealed Tau Ceti plans export into Literature Links.

Reads the export (`wikilean.brain.plans/v1`, see docs/BRAIN-PLANS.md) and the pinned inputs it
was built from, checks both against the export's manifest, and writes a folder of Literature
Links annotations (docs/BRAIN-PLANS-LL.md). Pointers and titles only: no README prose.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict

from scripts.brain_producers.common import BuildError, GitTree, _git_env, dumps, sha256_bytes, sha256_file
from scripts.tauceti_plans import parse
from scripts.tauceti_plans.build import FILES as EXPORT_FILES
from scripts.tauceti_plans.build import SCHEMA as EXPORT_SCHEMA
from scripts.tauceti_plans.build import git_work_tree_holding, logical_root, roadmap_title

SCHEMA = "wikilean.tauceti_plans_ll/v1"
PRODUCER = {"name": "scripts.tauceti_plans_ll", "version": "1.0.0"}
EXT = "org.wikilean.tauceti_plans"
ANNOTATIONS = "annotations"

ROADMAP_URI = "https://github.com/TauCetiProject/TauCetiRoadmap"
LIBRARIES = {  # resolution -> (repository, the export's input whose docs_commit is the version)
    "mathlib": ("https://github.com/leanprover-community/mathlib4", "mathlib_decls"),
    "tauceti": ("https://github.com/TauCetiProject/TauCeti", "tauceti_decls"),
}
STATUS = {"done": "formalized", "partial": "partial", "untouched": "not_formalized", "unassessed": None}
REL_ORDER = {None: 0, "hasPart": 1, "relatedMatch": 2}
ATTRIBUTION = {"work": "Literature Links (format)", "license": "Apache-2.0",
               "url": "https://github.com/mathlib-initiative/literature-links",
               "holder": "Literature Links contributors",
               "use": "the annotation format only; no code or text is copied"}


# ---------------------------------------------------------------- reading and checking

def _jsonl(blob: bytes) -> list[dict]:
    return [json.loads(ln) for ln in blob.decode("utf-8").splitlines() if ln]


def read_export(export: str) -> dict:
    """The export's manifest and rows, after checking every file and the logical root."""
    try:
        with open(os.path.join(export, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise BuildError(f"{export}: cannot read manifest.json ({e})") from None
    if manifest.get("schema") != EXPORT_SCHEMA:
        raise BuildError(f"{export}: schema {manifest.get('schema')!r} is not {EXPORT_SCHEMA}")
    if logical_root(manifest) != manifest.get("logical_root"):
        raise BuildError(f"{export}: manifest.json does not hash to its logical_root")
    rows = {}
    for name in EXPORT_FILES:
        want = (manifest.get("files") or {}).get(name) or {}
        try:
            with open(os.path.join(export, name), "rb") as f:
                blob = f.read()
        except OSError as e:
            raise BuildError(f"{export}: cannot read {name} ({e})") from None
        if sha256_bytes(blob) != want.get("sha256") or blob.count(b"\n") != want.get("rows"):
            raise BuildError(f"{export}: {name} does not match the manifest's sha256 and row count")
        if name in ("roadmaps.jsonl", "layers.jsonl", "decl_refs.jsonl"):
            rows[name] = _jsonl(blob)
    return {"manifest": manifest, **rows}


def read_inputs(inputs: str, manifest: dict) -> dict:
    """The roadmap checkout, trough's roadmaps.json and the two declaration indexes, each
    checked against the pin the export's manifest records."""
    pins = manifest["inputs"]
    rm = GitTree(os.path.join(inputs, "TauCetiRoadmap"))
    tr = GitTree(os.path.join(inputs, "trough"))
    for name, tree in (("TauCetiRoadmap", rm), ("trough", tr)):
        if tree.commit != pins[name]["commit"]:
            raise BuildError(f"{name}: HEAD {tree.commit} is not the export's {pins[name]['commit']}")
    path = "data/tauceti/roadmaps.json"
    if not tr.exists(path):
        raise BuildError(f"trough: {path} missing at {tr.commit}")
    raw = tr.read(path)
    if sha256_bytes(raw) != pins["trough"]["files"].get(path):
        raise BuildError(f"trough: {path} does not match the export's sha256")
    indexes = {}
    for lib, (_uri, inp) in LIBRARIES.items():
        rel = manifest["resolved_by"][inp]["path"]
        full = os.path.join(inputs, rel)
        if not os.path.exists(full) or sha256_file(full) != manifest["resolved_by"][inp]["sha256"]:
            raise BuildError(f"{rel}: missing or not the export's sha256")
        with open(full, encoding="utf-8") as f:
            indexes[lib] = set(json.load(f).get("declarations", {}))
    return {"tree": rm, "trough_roadmaps": json.loads(raw), "trough_rev": tr.commit, "indexes": indexes}


# ---------------------------------------------------------------- sections and Suggested.lean

def line_count(text: str) -> int:
    """Lines as the spec counts them: LF ends a line, and a last line without one counts too."""
    if "\r" in text.replace("\r\n", ""):
        raise BuildError("a bare CR would make the producer's line numbers disagree with the spec's")
    lines = parse.lines_of(text)
    return len(lines) - 1 if lines and lines[-1] == "" else len(lines)


def section_ranges(text: str) -> dict[int, tuple[int, int, int, str]]:
    """For each heading line: (start, end, level, title). A section runs from its heading to the
    line before the next heading of the same or a higher level, or to the end of the file."""
    n = line_count(text)
    hs = parse.headings(parse.lines_of(text))
    out = {}
    for i, h in enumerate(hs):
        end = n
        for later in hs[i + 1:]:
            if later["level"] <= h["level"]:
                end = later["line"] - 1
                break
        out[h["line"]] = (h["line"], end, h["level"], h["title"])
    return out


def module_of(path: str) -> str:
    # lakefile.toml globs `TauCetiRoadmap.*` and `Completed.+` from the repository root.
    return path[: -len(".lean")].replace("/", ".")


def suggested_owners(text: str, layer_keys: dict[str, str]) -> dict[str, str | None]:
    """Each declaration Suggested.lean makes -> its owning layer id (None: the roadmap), by the
    producer's rule: the nearest preceding marker heading names a unique README layer key."""
    found, markers = parse.lean_scan(text)
    marks = []
    for mk in markers:
        key = parse.layer_key(mk["title"])
        marks.append((mk["line"], layer_keys.get(key) if key else None))
    owners = {}
    for d in found:
        cur = None
        for ln, lid in marks:
            if ln > d["line"]:
                break
            cur = lid
        owners.setdefault(d["name"], cur)
    return owners


# ---------------------------------------------------------------- ends

def library_end(resolution: str, name: str, versions: dict) -> dict:
    uri, inp = LIBRARIES[resolution]
    end = {"type": "lean", "uri": uri, "location": {"declaration": name}}
    if versions[inp]:
        end["version"] = versions[inp]
    return end


def suggested_end(name: str, module: str, commit: str) -> dict:
    return {"type": "lean", "uri": ROADMAP_URI, "version": commit,
            "location": {"declaration": name, "module": module}}


# ---------------------------------------------------------------- main build

def title_at(repo: str, rev: str, readme: str, folder: str) -> str | None:
    """The roadmap title the plans producer's rule reads from `readme` at commit `rev`, or None
    when the commit or the file is absent. Offline: never fetches a missing object."""
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", "show", f"{rev}:{readme}"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=_git_env() | {"GIT_NO_LAZY_FETCH": "1"})
    if r.returncode != 0:
        return None
    lines = parse.lines_of(r.stdout.decode("utf-8"))
    return roadmap_title(lines, parse.headings(lines), folder)


def convert(export: dict, inp: dict) -> tuple[dict[str, list[dict]], dict, dict, list[dict]]:
    """Returns ({roadmap id: annotations}, the `_defaults.json` object, counts, rejected)."""
    manifest = export["manifest"]
    commit = manifest["inputs"]["TauCetiRoadmap"]["commit"]
    tree: GitTree = inp["tree"]
    versions = {i: manifest["inputs"][i].get("docs_commit") for _u, i in LIBRARIES.values()}
    roadmaps = {r["id"]: r for r in export["roadmaps.jsonl"]}
    layers_by_rm = defaultdict(list)
    for l in export["layers.jsonl"]:
        layers_by_rm[l["roadmap"]].append(l)
    refs_by_owner = defaultdict(list)
    for r in export["decl_refs.jsonl"]:
        refs_by_owner[r["layer"] or r["roadmap"]].append(r)

    # Subjects: every roadmap README (whole) and every layer section.
    subjects, status = {}, {}
    for rid, rm in roadmaps.items():
        readme = rm["source"]["path"]
        if rm["source"]["commit"] != commit or not tree.exists(readme):
            raise BuildError(f"{rid}: {readme} is not in TauCetiRoadmap at {commit}")
        subjects[rid] = {"location": {"path": readme}, "title": rm["title"]}
        if not layers_by_rm[rid]:
            continue
        ranges = section_ranges(tree.read(readme).decode("utf-8"))
        for l in layers_by_rm[rid]:
            src = l["source"]
            sec = ranges.get(src["line"])
            if src["path"] != readme or src["commit"] != commit or sec is None or sec[3] != l["title"]:
                raise BuildError(f"{l['id']}: no heading {l['title']!r} at {readme}:{src['line']}")
            subjects[l["id"]] = {"location": {"path": readme, "lines": [sec[0], sec[1]]}, "title": l["title"]}
            if l["state"] not in STATUS:
                raise BuildError(f"{l['id']}: unknown state {l['state']!r}")
            status[l["id"]] = STATUS[l["state"]]

    links = defaultdict(set)       # owner id -> {(relation, canonical object end)}
    omitted = defaultdict(lambda: defaultdict(set))
    counts = Counter()
    rejected: list[dict] = []
    for rid in sorted(roadmaps):
        readme = roadmaps[rid]["source"]["path"]
        sug = readme[: -len("README.md")] + "Suggested.lean"
        declared: dict[str, str | None] = {}
        if tree.exists(sug):
            keys = Counter(l["key"] for l in layers_by_rm[rid])
            unique = {l["key"]: l["id"] for l in layers_by_rm[rid] if keys[l["key"]] == 1}
            declared = suggested_owners(tree.read(sug).decode("utf-8"), unique)
        owners = [rid] + [l["id"] for l in layers_by_rm[rid]]
        for owner in owners:
            for r in refs_by_owner.get(owner, []):
                name, res = r["name"], r["resolution"]
                # A Suggested.lean declaration: the export marks it by its evidence path; one whose
                # first occurrence is in the README is recovered by re-scanning the file.
                is_sug = r["evidence"]["path"] == sug or (name in declared and (declared[name] or rid) == owner)
                if r["evidence"]["path"] == sug and (declared.get(name) or rid) != owner:
                    raise BuildError(f"{r['id']}: not a declaration of {sug} under that owner at {commit}")
                if is_sug:
                    links[owner].add(("hasPart", dumps(suggested_end(name, module_of(sug), commit))))
                    counts["suggested_rows_from_readme_evidence"] += r["evidence"]["path"] != sug
                if res in LIBRARIES:
                    end = library_end(res, r.get("resolved_name") or name, versions)
                    links[owner].add(("relatedMatch", dumps(end)))
                    counts["objects_without_version"] += "version" not in end
                elif not is_sug:
                    omitted[owner][res].add(name)

    # Roadmap -> its status-named results in trough, when the name is a declaration-index entry.
    # trough read the roadmaps at its own TauCetiRoadmap rev, usually behind the export's commit.
    # Its results attach only to the same roadmap: the path must exist at the export's commit, and
    # the README title, parsed by the producer's own rule, must be equal at both commits. Anything
    # else is rejected with a reason and never re-attached to whatever now sits at that path.
    trev = inp["trough_roadmaps"].get("rev")
    trev = trev if isinstance(trev, str) and re.fullmatch(r"[0-9a-f]{40}", trev) else None
    for t in inp["trough_roadmaps"].get("roadmaps", []):
        rid = f"tauceti:{t['path']}"
        reason = None
        if rid not in roadmaps:
            reason = "roadmap_not_at_export_commit"
        elif trev is None:
            reason = "trough_rev_unknown"
        else:
            then = title_at(tree.repo, trev, roadmaps[rid]["source"]["path"], t["path"])
            if then is None:
                reason = "readme_not_at_trough_rev"
            elif then != roadmaps[rid]["title"]:
                reason = "roadmap_changed_since_trough_rev"
        if reason:
            rejected.append({"kind": "trough_roadmap", "path": t["path"], "reason": reason,
                             "trough_rev": trev, "names": len(t.get("names", []))})
            counts[f"trough_rejected/{reason}"] += 1
            continue
        for name in t.get("names", []):
            lib = next((lib for lib in LIBRARIES if name in inp["indexes"][lib]), None)
            if lib is None:
                omitted[rid]["unresolved_results"].add(name)
                counts["trough_results_unresolved"] += 1
                continue
            end = library_end(lib, name, versions)
            links[rid].add(("hasPart", dumps(end)))
            counts["trough_results_resolved"] += 1
            counts["objects_without_version"] += "version" not in end

    out: dict[str, list[dict]] = {}
    for rid in sorted(roadmaps):
        anns = []
        for owner in [rid] + sorted(l["id"] for l in layers_by_rm[rid]):
            is_layer = owner != rid
            items = sorted(links.get(owner, ()), key=lambda x: (REL_ORDER[x[0]], x[1]))
            ext = {k: sorted(v) for k, v in sorted(omitted.get(owner, {}).items())}
            if not items and (is_layer or ext):
                items = [(None, None)]
            for i, (rel, obj) in enumerate(items):
                a = {"subject": [subjects[owner]], "object": [json.loads(obj)] if obj else [], "id": owner}
                if rel:
                    a["relation"] = rel
                if is_layer and status[owner]:
                    a["status"] = status[owner]
                if i == 0 and ext:
                    a["extensions"] = {EXT: ext}
                anns.append(a)
                counts[f"annotations/{rel or 'unlinked'}"] += 1
                counts["roadmap_annotations" if not is_layer else "layer_annotations"] += 1
            if is_layer:
                counts["layers"] += 1
                counts[f"layers_by_status/{status[owner] or 'none'}"] += 1
                counts["unlinked_layers"] += not links.get(owner)
            for k, v in ext.items():
                counts[f"omitted_names/{k}"] += len(v)
        out[rid] = anns
    defaults = {"subject": {"type": "generic", "uri": ROADMAP_URI, "version": commit}}
    rejected.sort(key=lambda r: (r["path"], r["reason"]))
    return out, defaults, dict(sorted(counts.items())), rejected


def file_of(rid: str) -> str:
    """`tauceti:<Top>/<Name>[/<Nested>...]` -> `<Top>/<Name>.jsonl`, nested roadmaps included."""
    top, name = rid[len("tauceti:"):].split("/")[:2]
    return f"{top}/{name}.jsonl"


def build(export: str, inputs: str, out: str) -> dict:
    # The output is never committed, as for the producers.
    tree_root = git_work_tree_holding(out)
    if tree_root is not None:
        raise BuildError(f"--out {out} lies inside the git work tree {tree_root}; write it elsewhere")
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise BuildError(f"--out {out} exists and is not an empty directory; stale files would be scanned")
    ex = read_export(export)
    inp = read_inputs(inputs, ex["manifest"])
    by_rm, defaults, counts, rejected = convert(ex, inp)

    blobs: dict[str, bytes] = {f"{ANNOTATIONS}/_defaults.json": (dumps(defaults) + "\n").encode("utf-8")}
    grouped = defaultdict(list)
    for rid in sorted(by_rm):
        grouped[file_of(rid)] += by_rm[rid]
    for path, anns in sorted(grouped.items()):
        blobs[f"{ANNOTATIONS}/{path}"] = "".join(dumps(a) + "\n" for a in anns).encode("utf-8")

    src = ex["manifest"]
    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "source_export": {"schema": src["schema"], "producer": src["producer"],
                          "logical_root": src["logical_root"]},
        "inputs": {
            "TauCetiRoadmap": {"repo": src["inputs"]["TauCetiRoadmap"]["repo"],
                               "commit": src["inputs"]["TauCetiRoadmap"]["commit"]},
            "trough": {"repo": src["inputs"]["trough"]["repo"], "commit": inp["trough_rev"],
                       "roadmaps_json_sha256": src["inputs"]["trough"]["files"]["data/tauceti/roadmaps.json"],
                       "roadmaps_json_rev": inp["trough_roadmaps"].get("rev")},
            **{i: {"sha256": src["resolved_by"][i]["sha256"], "docs_commit": src["inputs"][i].get("docs_commit")}
               for _u, i in LIBRARIES.values()},
        },
        "annotations_root": ANNOTATIONS,
        "files": {p: {"rows": b.count(b"\n"), "bytes": len(b), "sha256": sha256_bytes(b)}
                  for p, b in sorted(blobs.items())},
        "counts": counts,
        "rejected": rejected,
        "attribution": src["attribution"] + [ATTRIBUTION],
    }
    manifest["logical_root"] = sha256_bytes(dumps(manifest).encode("utf-8"))
    blobs["manifest.json"] = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1,
                                         allow_nan=False) + "\n").encode("utf-8")
    for rel, blob in blobs.items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
    return manifest
