#!/usr/bin/env python3
"""The plans layer of the Brain base graph (brain/SCHEMA.md, "Plans layer").

Reads the sealed export of the Tau Ceti plan producer (`scripts/tauceti_plans`, contract
docs/BRAIN-PLANS.md), staged under brain/data/tauceti_plans/ by site/ops/brain_producers.py,
and turns it into plan nodes, their containers under path:Plans, and their edges. An edge
whose endpoint is not a node of this build, or whose evidence no reader can follow, is
held (brain/data/held_edges.jsonl), never dropped: every build retries every held edge.

Live builds only. Of the reducer-input inventories only reducer-inputs-v1.json (the
nightly's) declares the export, so a replay context never sees it, and build_common imports
this module only on the live path: replay's declared code closure stays as it is.

The export's own checks are repeated here, not trusted: every file's sha256 and size against
the manifest, the manifest's logical_root recomputed, rows sorted by unique id, and every
reference between rows resolved. Any failure refuses the build (PlansExportError).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EXPORT_SCHEMA = "wikilean.brain.plans/v1"
EXPORT_FILES = (
    "decl_refs.jsonl",
    "edges.jsonl",
    "layers.jsonl",
    "rejected.jsonl",
    "roadmaps.jsonl",
    "xrefs.jsonl",
)
MANIFEST = "manifest.json"
CLOCK_KEYS = frozenset({"fetched_at"})

SOURCE = "tauceti_plans"          # catalog/data/source_registry.json frontier_sources key
PLANS_ROOT = "path:Plans"         # reserved: no library may claim it (brain/SCHEMA.md)
TAUCETI_ROOT = "path:Plans/TauCeti"
UNCLASSIFIED = "unclassified"
PLAN_PREFIX = "tauceti:"

HELD_SCHEMA = "wikilean.brain.held-edges/v1"
HELD_REASONS = ("endpoint_missing", "evidence_not_public")
HELD_NAME = "held_edges.jsonl"
HELD_PREVIOUS_NAME = "held_edges.previous.jsonl"

# A topic is an arXiv category (math.NT); anything else would make an odd path id, so such a
# roadmap is placed under `unclassified` and keeps its topic verbatim on the node.
_TOPIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,63}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_LIBRARIES = {"mathlib": "Mathlib", "tauceti": "TauCeti"}
_XREF_KINDS = {"stacks": ("xref", "xref:stacks:"), "arxiv": ("cites", "lit:")}


class PlansExportError(Exception):
    """The staged export is not a sealed, self-consistent plans export."""


# ---- canonical bytes (the producer's, scripts/brain_producers/common.py) ----------------

def canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def strip_clocks(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_clocks(v) for k, v in obj.items() if k not in CLOCK_KEYS}
    if isinstance(obj, list):
        return [strip_clocks(v) for v in obj]
    return obj


def logical_root(manifest: dict) -> str:
    """sha256 of the canonical manifest without `logical_root` and every `fetched_at`."""
    body = {k: v for k, v in manifest.items() if k != "logical_root"}
    return hashlib.sha256(canonical_dumps(strip_clocks(body)).encode("utf-8")).hexdigest()


def jsonl_bytes(rows: Iterable[dict]) -> bytes:
    return b"".join((canonical_dumps(r) + "\n").encode("utf-8") for r in rows)


# ---- reading the export ------------------------------------------------------------------

@dataclass(frozen=True)
class PlansExport:
    directory: Path
    manifest: dict
    logical_root: str
    rows: dict[str, list[dict]]


def candidate_keys(export: PlansExport) -> set[tuple[str, str, str]]:
    """Every (kind, src, dst) the export proposes, derived from its rows alone.

    Deliberately independent of build_layer: test_acceptance (P17) checks that each of these
    is either emitted or held, so a build that drops one is caught whatever it wrote.
    """
    keys: set[tuple[str, str, str]] = set()
    for r in export.rows["edges.jsonl"]:
        keys.add(("depends", r["source"], r["target"]))
    for r in export.rows["decl_refs.jsonl"]:
        if r.get("resolution") in _LIBRARIES:
            name = r.get("resolved_name") or r["name"]
            keys.add(("mentions", r.get("layer") or r["roadmap"],
                      f"decl:{_LIBRARIES[r['resolution']]}:{name}"))
    for r in export.rows["xrefs.jsonl"]:
        kind = _XREF_KINDS[r["system"]][0]
        keys.add((kind, r.get("layer") or r["roadmap"], r["xref"]))
    return keys


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlansExportError(f"cannot open {path.name}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PlansExportError(f"{path.name} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_rows(name: str, data: bytes) -> list[dict]:
    if data and not data.endswith(b"\n"):
        raise PlansExportError(f"{name} does not end with a newline")
    rows: list[dict] = []
    previous: str | None = None
    for lineno, line in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlansExportError(f"{name}:{lineno} is not JSON") from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise PlansExportError(f"{name}:{lineno} has no string id")
        if previous is not None and row["id"] <= previous:
            raise PlansExportError(f"{name}:{lineno} is out of order or repeats an id")
        previous = row["id"]
        rows.append(row)
    return rows


def load_export(directory: Path) -> PlansExport | None:
    """The verified export at `directory`, or None when nothing is staged there."""
    directory = Path(directory)
    try:
        st = directory.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PlansExportError(f"{directory} is not a directory")
    # Hidden entries (a Finder .DS_Store) are no part of an export, which has none, and no
    # release binds them (reducer-inputs-v1.json declares *.jsonl and manifest.json).
    entries = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    expected = sorted((*EXPORT_FILES, MANIFEST))
    if entries != expected:
        extra = sorted(set(entries) - set(expected))
        missing = sorted(set(expected) - set(entries))
        raise PlansExportError(
            f"{directory} must hold exactly the export's files"
            + (f"; unexpected {extra}" if extra else "")
            + (f"; missing {missing}" if missing else ""))
    try:
        manifest = json.loads(_read_regular(directory / MANIFEST))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PlansExportError("manifest.json is not JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_SCHEMA:
        raise PlansExportError(f"manifest schema is not {EXPORT_SCHEMA}")
    if manifest.get("clock_fields") != sorted(CLOCK_KEYS):
        raise PlansExportError(f"manifest clock_fields is not {sorted(CLOCK_KEYS)}")
    root = manifest.get("logical_root")
    if not isinstance(root, str) or not _HEX64.match(root):
        raise PlansExportError("manifest has no logical_root")
    if logical_root(manifest) != root:
        raise PlansExportError("manifest logical_root does not match its contents")
    files = manifest.get("files")
    if not isinstance(files, dict) or sorted(files) != sorted(EXPORT_FILES):
        raise PlansExportError(f"manifest files must be exactly {list(EXPORT_FILES)}")
    rows: dict[str, list[dict]] = {}
    for name in EXPORT_FILES:
        want = files[name]
        data = _read_regular(directory / name)
        if (not isinstance(want, dict) or len(data) != want.get("bytes")
                or hashlib.sha256(data).hexdigest() != want.get("sha256")):
            raise PlansExportError(f"{name} does not match its manifest entry")
        rows[name] = _parse_rows(name, data)
        if len(rows[name]) != want.get("rows"):
            raise PlansExportError(f"{name} row count does not match its manifest entry")
    return PlansExport(directory=directory, manifest=manifest, logical_root=root, rows=rows)


# ---- building the layer -----------------------------------------------------------------

@dataclass
class PlansLayer:
    containers: list[dict] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _edge(src: str, dst: str, kind: str, method: str, pin: str, evidence: dict) -> dict:
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": SOURCE, "method": method, "pin": pin},
            "confidence": "high", "evidence": evidence}


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != []}


def _owner(row: dict, file: str, plan_ids: set[str]) -> str:
    owner = row.get("layer") or row.get("roadmap")
    if owner not in plan_ids:
        raise PlansExportError(f"{file} row {row['id']} names no roadmap or layer of the export")
    return owner


def validate_export(export: PlansExport) -> None:
    """Everything build_layer refuses, short of collisions with a build's other nodes.

    The producer step runs this before publishing or staging an export, so a structurally
    bad export never reaches the build, where it would refuse the whole nightly.
    """
    build_layer(export, node_ids=set())


def build_layer(export: PlansExport, *, node_ids: set[str]) -> PlansLayer:
    """Plan nodes, containers and edges; `node_ids` are every other node of the build."""
    root = export.logical_root
    rows = export.rows
    roadmaps = {r["id"]: r for r in rows["roadmaps.jsonl"]}
    layers = {r["id"]: r for r in rows["layers.jsonl"]}

    for rid, r in roadmaps.items():
        if not rid.startswith(PLAN_PREFIX) or "#" in rid:
            raise PlansExportError(f"roadmap id {rid!r} is not tauceti:<folder>")
        if r.get("parent") is not None and r["parent"] not in roadmaps:
            raise PlansExportError(f"roadmap {rid} has an unknown parent")
    for rid in roadmaps:  # parents form a forest
        seen, cur = set(), rid
        while cur is not None:
            if cur in seen:
                raise PlansExportError(f"roadmap {rid} is in a parent cycle")
            seen.add(cur)
            cur = roadmaps[cur].get("parent")
    for lid, r in layers.items():
        if r.get("roadmap") not in roadmaps or not lid.startswith(r["roadmap"] + "#"):
            raise PlansExportError(f"layer {lid} does not belong to a roadmap of the export")
    plan_ids = set(roadmaps) | set(layers)
    clash = sorted(plan_ids & node_ids)
    if clash:
        raise PlansExportError(f"plan ids collide with other nodes, e.g. {clash[0]}")

    out = PlansLayer()
    stats: dict[str, Any] = {
        "roadmaps": len(roadmaps), "layers": len(layers),
        "layers_by_state": dict(sorted(Counter(r.get("state") for r in layers.values()).items())),
        "topics_unplaceable": 0, "mentions_duplicate_rows": 0,
    }

    # ---- containers: path:Plans -> path:Plans/TauCeti -> path:Plans/TauCeti/<topic> -------
    def topic_of(r: dict) -> str:
        topic = r.get("topic")
        if isinstance(topic, str) and _TOPIC_RE.match(topic):
            return topic
        if topic is not None:
            stats["topics_unplaceable"] += 1
        return UNCLASSIFIED

    top_level = sorted(rid for rid, r in roadmaps.items() if r.get("parent") is None)
    topic_roadmaps: dict[str, list[str]] = defaultdict(list)
    for rid in top_level:
        topic_roadmaps[topic_of(roadmaps[rid])].append(rid)
    subtree: dict[str, Counter] = defaultdict(Counter)   # container -> {roadmap, layer}
    top_of: dict[str, str] = {}
    for rid in roadmaps:
        cur = rid
        while roadmaps[cur].get("parent") is not None:
            cur = roadmaps[cur]["parent"]
        top_of[rid] = cur
    for rid in roadmaps:
        topic = topic_of(roadmaps[top_of[rid]])
        for c in (PLANS_ROOT, TAUCETI_ROOT, f"{TAUCETI_ROOT}/{topic}"):
            subtree[c]["roadmap"] += 1
    for r in layers.values():
        topic = topic_of(roadmaps[top_of[r["roadmap"]]])
        for c in (PLANS_ROOT, TAUCETI_ROOT, f"{TAUCETI_ROOT}/{topic}"):
            subtree[c]["layer"] += 1

    def container(cid: str, label: str) -> dict:
        return {"id": cid, "type": "container", "label": label,
                "n_roadmaps": subtree[cid]["roadmap"], "n_layers": subtree[cid]["layer"]}

    tree = "plans export tree (roadmaps.jsonl)"
    out.containers.append(container(PLANS_ROOT, "Plans"))
    out.containers.append(container(TAUCETI_ROOT, "Tau Ceti"))
    out.edges.append(_edge(PLANS_ROOT, TAUCETI_ROOT, "contains", tree, root, {}))
    for topic in sorted(topic_roadmaps):
        cid = f"{TAUCETI_ROOT}/{topic}"
        out.containers.append(container(cid, topic))
        out.edges.append(_edge(TAUCETI_ROOT, cid, "contains", tree, root, {}))
        for rid in topic_roadmaps[topic]:
            out.edges.append(_edge(cid, rid, "contains", tree, root, {}))
    for rid in sorted(roadmaps):
        parent = roadmaps[rid].get("parent")
        if parent is not None:
            out.edges.append(_edge(parent, rid, "contains", tree, root, {}))
    for lid in sorted(layers):
        out.edges.append(_edge(layers[lid]["roadmap"], lid, "contains",
                               "plans export tree (layers.jsonl)", root, {}))

    def hold(edge: dict, reason: str, row_id: str, missing: str | None = None) -> None:
        out.held.append(_prune({"edge": edge, "reason": reason, "missing": missing,
                                "logical_root": root, "row": row_id}))

    # ---- depends: consumer -> prerequisite, exactly as the export emits it ---------------
    for r in rows["edges.jsonl"]:
        if r.get("kind") != "depends":
            raise PlansExportError(f"edges.jsonl row {r['id']} is not a depends edge")
        src, dst = r.get("source"), r.get("target")
        edge = _edge(src, dst, "depends", "atlas-verified-line", root, _prune({
            "evidence": r.get("evidence"),
            "evidence_public": r.get("evidence_public"),
            "more_evidence": r.get("more_evidence"),
            "atlas_kinds": r.get("atlas_kinds"),
        }))
        missing = next((x for x in (src, dst) if x not in plan_ids), None)
        if missing is not None:
            hold(edge, "endpoint_missing", r["id"], missing)
        elif r.get("evidence_public") is not True:
            hold(edge, "evidence_not_public", r["id"])
        else:
            out.edges.append(edge)

    # ---- mentions (resolved names); planned and unqualified names stay attributes --------
    planned: dict[str, list[str]] = defaultdict(list)
    unqualified: dict[str, list[dict]] = defaultdict(list)
    mentions: dict[tuple[str, str], tuple[dict, str]] = {}
    for r in rows["decl_refs.jsonl"]:
        owner = _owner(r, "decl_refs.jsonl", plan_ids)
        resolution = r.get("resolution")
        if resolution == "planned":
            planned[owner].append(r["name"])
        elif resolution == "unqualified":
            unqualified[owner].append(_prune({
                "name": r["name"], "candidates": r.get("candidates"),
                "candidates_total": r.get("candidates_total"),
                "candidate_libraries": r.get("candidate_libraries"),
            }))
        elif resolution in _LIBRARIES:
            name = r.get("resolved_name") or r["name"]
            dst = f"decl:{_LIBRARIES[resolution]}:{name}"
            method = "declared-scope" if r.get("via_namespace") else "exact"
            edge = _edge(owner, dst, "mentions", method, root, _prune({
                "role": "plan", "resolution": resolution,
                "via_namespace": r.get("via_namespace"),
                "resolved_name": r.get("resolved_name"),
                "occurrences": r.get("occurrences"), "pointer": r.get("evidence"),
            }))
            key = (owner, dst)
            if key in mentions:
                # Two names of one owner resolve to one declaration (`Foo.bar` through a
                # declared namespace and `N.Foo.bar` written out): one edge, the exact one first.
                stats["mentions_duplicate_rows"] += 1
                if method == "exact" and mentions[key][0]["provenance"]["method"] != "exact":
                    mentions[key] = (edge, r["id"])
                continue
            mentions[key] = (edge, r["id"])
        else:
            raise PlansExportError(f"decl_refs.jsonl row {r['id']} has resolution {resolution!r}")
    for (_owner_id, dst), (edge, row_id) in sorted(mentions.items()):
        if dst in node_ids:
            out.edges.append(edge)
        else:
            hold(edge, "endpoint_missing", row_id, dst)

    # ---- xref (Stacks) and cites (arXiv), routed by the row's system ---------------------
    for r in rows["xrefs.jsonl"]:
        owner = _owner(r, "xrefs.jsonl", plan_ids)
        kind_prefix = _XREF_KINDS.get(r.get("system"))
        target = r.get("xref")
        if kind_prefix is None or not isinstance(target, str) \
                or not target.startswith(kind_prefix[1]):
            raise PlansExportError(f"xrefs.jsonl row {r['id']} has an unknown system or target")
        edge = _edge(owner, target, kind_prefix[0], "pattern-scan", root, _prune({
            "system": r["system"], "occurrences": r.get("occurrences"),
            "pointer": r.get("evidence"),
        }))
        if target in node_ids:
            out.edges.append(edge)
        else:
            hold(edge, "endpoint_missing", r["id"], target)

    # ---- plan nodes ----------------------------------------------------------------------
    for rid in sorted(roadmaps):
        r = roadmaps[rid]
        out.nodes.append(_prune({
            "id": rid, "type": "plan", "plan_kind": "roadmap", "label": r.get("title") or rid,
            "origin": r.get("origin"), "topic": r.get("topic"), "source": r.get("source"),
            "planned_names": sorted(set(planned.get(rid, []))) or None,
            "unqualified_names": sorted(unqualified.get(rid, []), key=lambda u: u["name"]) or None,
        }))
    for lid in sorted(layers):
        r = layers[lid]
        out.nodes.append(_prune({
            "id": lid, "type": "plan", "plan_kind": "layer", "label": r.get("title") or lid,
            "key": r.get("key"), "state": r.get("state"), "ordinal": r.get("ordinal"),
            "status_source": r.get("status_source"), "source": r.get("source"),
            "planned_names": sorted(set(planned.get(lid, []))) or None,
            "unqualified_names": sorted(unqualified.get(lid, []), key=lambda u: u["name"]) or None,
        }))
    out.nodes.sort(key=lambda n: n["id"])
    out.containers.sort(key=lambda n: n["id"])
    out.held.sort(key=_held_key)

    stats["edges"] = dict(sorted(Counter(e["kind"] for e in out.edges).items()))
    stats["held"] = held_counts(out.held)
    stats["planned_names"] = sum(len(set(v)) for v in planned.values())
    stats["unqualified_names"] = sum(len(v) for v in unqualified.values())
    out.stats = stats
    return out


# ---- the held list ----------------------------------------------------------------------

def _held_key(h: dict) -> tuple[str, str, str, str]:
    e = h["edge"]
    return (e["kind"], e["src"], e["dst"], h["reason"])


def edge_key(edge: dict) -> tuple[str, str, str]:
    return (edge["kind"], edge["src"], edge["dst"])


def held_counts(held: Iterable[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for h in held:
        counts[h["edge"]["kind"]][h["reason"]] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(counts.items())}


def held_summary(held: list[dict]) -> dict:
    """What a release records of the held list: counts by kind and reason, and its digest."""
    return {"rows": len(held), "counts": held_counts(held),
            "sha256": hashlib.sha256(jsonl_bytes(held)).hexdigest()}


def write_held(path: Path, held: list[dict], *, logical_root: str | None,
               snapshot_id: str | None) -> None:
    """One `_meta` line, then the canonical rows the summary's sha256 covers."""
    meta = {"schema": HELD_SCHEMA, "logical_root": logical_root,
            **({"snapshot_id": snapshot_id} if snapshot_id else {}),
            **held_summary(held)}
    with Path(path).open("wb") as fh:
        fh.write((canonical_dumps({"_meta": meta}) + "\n").encode("utf-8"))
        fh.write(jsonl_bytes(held))


def read_held(path: Path) -> tuple[dict, list[dict]]:
    """(meta, rows) of a held list; raises ValueError on any shape problem."""
    lines = Path(path).read_bytes().split(b"\n")
    if not lines or lines[-1] != b"":
        raise ValueError(f"{Path(path).name} does not end with a newline")
    first = json.loads(lines[0])
    if not isinstance(first, dict) or not isinstance(first.get("_meta"), dict) \
            or first["_meta"].get("schema") != HELD_SCHEMA:
        raise ValueError(f"{Path(path).name} has no {HELD_SCHEMA} _meta line")
    rows = [json.loads(line) for line in lines[1:-1]]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("edge"), dict) \
                or row.get("reason") not in HELD_REASONS or not isinstance(row.get("row"), str):
            raise ValueError(f"{Path(path).name} has a malformed held row")
    return first["_meta"], rows


# ---- the acceptance check (brain/test_acceptance.py P17) --------------------------------

def check_plans(*, nodes_meta: dict, n_plan_nodes: int, node_ids: set[str],
                plan_edge_keys: set[tuple[str, str, str]], data_dir: Path
                ) -> tuple[bool | str, list[str]]:
    """The plans layer and its held list, against the export and the previous held list.

    Completeness, per build: every edge the staged export proposes (candidate_keys, derived
    from its rows alone) is either emitted or held, and nothing else is emitted. Accounting,
    across builds: each held edge of the previous build, by kind and reason, is still held,
    emitted this build, or gone (the current export no longer proposes it: its row went, or
    it now resolves elsewhere). So `previous - emitted - gone + newly held = current`; a held
    edge the export still proposes can only disappear by failing completeness.
    """
    plans = nodes_meta.get("plans") or {}
    export_dir = Path(data_dir) / "tauceti_plans"
    held_path = Path(data_dir) / HELD_NAME
    previous_path = Path(data_dir) / HELD_PREVIOUS_NAME
    problems: list[str] = []

    if plans.get("state") != "present":
        if export_dir.exists():
            return False, ["brain/data/tauceti_plans/ is staged but the build did not read it "
                           "(rerun python3 brain/build_snapshot.py)"]
        if n_plan_nodes:
            return False, [f"{n_plan_nodes} plan nodes without a plans export"]
        if held_path.exists():
            try:
                _meta, rows = read_held(held_path)
            except (OSError, ValueError) as exc:
                return False, [f"held list unreadable: {exc}"]
            if rows:
                return False, [f"{len(rows)} held edges without a plans export"]
        return "skip", [f"no plans export: {plans.get('reason') or 'nodes meta has no plans block'}"]

    root = plans.get("logical_root")
    try:
        export = load_export(export_dir)
    except PlansExportError as exc:
        return False, [f"staged export invalid: {exc}"]
    if export is None or export.logical_root != root:
        return False, ["the staged export is not the one the build read "
                       f"(build {str(root)[:12]}, staged "
                       f"{export.logical_root[:12] if export else 'none'})"]
    expected_nodes = len(export.rows["roadmaps.jsonl"]) + len(export.rows["layers.jsonl"])
    if n_plan_nodes != expected_nodes:
        problems.append(f"{n_plan_nodes} plan nodes, the export has {expected_nodes} "
                        "roadmaps and layers")

    try:
        meta, held = read_held(held_path)
    except FileNotFoundError:
        return False, problems + ["brain/data/held_edges.jsonl missing"]
    except (OSError, ValueError) as exc:
        return False, problems + [f"held list unreadable: {exc}"]
    summary = held_summary(held)
    if meta.get("logical_root") != root:
        problems.append("held list is from another export")
    if nodes_meta.get("snapshot_id") and meta.get("snapshot_id") != nodes_meta["snapshot_id"]:
        problems.append("held list is from another snapshot")
    for key in ("rows", "counts", "sha256"):
        if meta.get(key) != summary[key] or (plans.get("held") or {}).get(key) != summary[key]:
            problems.append(f"held list {key} does not match its rows and the nodes meta")
    current_keys: dict[tuple[str, str, str], str] = {}
    bad: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for h in held:
        e = h["edge"]
        k = edge_key(e)
        if k in current_keys:
            bad["is held twice"].append(k)
        if h.get("logical_root") != root:
            bad["carries another logical_root"].append(k)
        if k in plan_edge_keys:
            bad["is also an emitted edge"].append(k)
        if h["reason"] == "endpoint_missing":
            if h.get("missing") not in (e["src"], e["dst"]) or h.get("missing") in node_ids:
                bad["names a missing endpoint that is a node or no endpoint"].append(k)
        # The build's rule: only `evidence_public: true` is public (a missing field is not).
        elif e["kind"] != "depends" or (e.get("evidence") or {}).get("evidence_public") is True:
            bad["is held as evidence_not_public on a public or non-depends edge"].append(k)
        current_keys[k] = h["reason"]
    for what, keys in sorted(bad.items()):
        problems.append(f"{len(keys)} held edges {what}, e.g. {keys[:3]}")

    # ---- completeness: the export's every candidate is emitted or held, and no more ------
    try:
        candidates = candidate_keys(export)
    except (KeyError, TypeError) as exc:
        return False, problems + [f"staged export rows are malformed: {exc!r}"]
    proposed = {k for k in plan_edge_keys if k[0] != "contains"} | set(current_keys)
    dropped = sorted(candidates - proposed)
    invented = sorted(proposed - candidates)
    if dropped:
        problems.append(f"{len(dropped)} edges the export proposes are neither emitted nor "
                        f"held, e.g. {dropped[:3]}")
    if invented:
        problems.append(f"{len(invented)} plan edges the export does not propose, "
                        f"e.g. {invented[:3]}")

    details = [f"export {root[:12]}: {n_plan_nodes} plan nodes, {len(candidates)} candidate "
               f"edges, {len(held)} held {json.dumps(summary['counts'], sort_keys=True)}"]
    if not previous_path.exists():
        details.append("no previous held list (first build with plans): accounting starts now")
    else:
        try:
            _pmeta, previous = read_held(previous_path)
        except (OSError, ValueError) as exc:
            return False, problems + [f"previous held list unreadable: {exc}"]
        table: dict[tuple[str, str], Counter] = defaultdict(Counter)
        lost: list[str] = []
        for h in previous:
            k, reason = edge_key(h["edge"]), h["reason"]
            if k in current_keys and current_keys[k] == reason:
                table[(k[0], reason)]["carried"] += 1
            elif k in current_keys or k in plan_edge_keys:
                table[(k[0], reason)]["emitted"] += 1   # emitted, or re-held for another reason
            elif k not in candidates:
                table[(k[0], reason)]["gone"] += 1      # the export no longer proposes it
            else:
                lost.append(f"{k} ({reason})")
        prev_keys = {(edge_key(h["edge"]), h["reason"]) for h in previous}
        for h in held:
            if (edge_key(h["edge"]), h["reason"]) not in prev_keys:
                table[(h["edge"]["kind"], h["reason"])]["new"] += 1
        prev_counts = held_counts(previous)
        for (kind, reason), c in sorted(table.items()):
            before = prev_counts.get(kind, {}).get(reason, 0)
            after = summary["counts"].get(kind, {}).get(reason, 0)
            if before - c["emitted"] - c["gone"] + c["new"] != after:
                problems.append(f"{kind}/{reason}: {before} - {c['emitted']} emitted - "
                                f"{c['gone']} gone + {c['new']} new != {after}")
            details.append(f"{kind}/{reason}: {before} before, {c['emitted']} emitted, "
                           f"{c['gone']} gone (no longer proposed), {c['new']} newly held, "
                           f"{after} now")
        if lost:
            problems.append(f"{len(lost)} held edges disappeared without being emitted, "
                            f"e.g. {lost[:3]}")
    return (not problems), problems + details
