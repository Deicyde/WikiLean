#!/usr/bin/env python3
"""BRAIN v3 builder — cells, supercells, synapses.

Derives the v3 ATOM layer from the v1/v2 ORGAN layer (`brain/data/{nodes,edges}.jsonl`).
Particles — Mathlib decls, Wikidata concepts, external DB pages, WikiLean articles,
arXiv statements — merge into CELLS; modules become SUPERCELLS that cells render
inside; every weak bond between two cells aggregates into ONE SYNAPSE that retains
all of its traces.

Contract: `brain/SCHEMA.md` (v3 section) — normative. Design: `docs/BRAIN-V3.md`.

The merge is a FUNCTION, never a transitive closure (SCHEMA rules 1-5). Measured,
a closure fuses Module<->EuclideanSpace<->plane into one 28-organ cell and, via
coarse DLMF pages, produces a 212-organ blob. `merge()` is where that is enforced;
`test_cells.py` (C1-C3) is where it is proven.

Outputs:
  brain/data/cells.jsonl     {id, anchor, label, organs[], supercells[], f, xy}
  brain/data/synapses.jsonl  {src, dst, weight, kinds{}, traces[]}

Usage:
  python3 brain/build_cells.py [--no-layout] [--stats]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRAIN_DATA = HERE / "data"
CATALOG_DATA = ROOT / "catalog" / "data"
BOT_STATE = ROOT / "bot" / "state"

NODES_IN = BRAIN_DATA / "nodes.jsonl"
EDGES_IN = BRAIN_DATA / "edges.jsonl"
EDGES_LINKS_IN = BRAIN_DATA / "edges_links.jsonl"      # optional (gitignored, 146MB)
REJECTED_IN = BRAIN_DATA / "discovery_rejected.jsonl"  # optional
OVERRIDES_IN = CATALOG_DATA / "grounding_overrides.jsonl"

CELLS_OUT = BRAIN_DATA / "cells.jsonl"
SYNAPSES_OUT = BRAIN_DATA / "synapses.jsonl"
REVIEW_OUT = BRAIN_DATA / "cell_review.jsonl"  # tagger-quality worklist

# ---- SCHEMA v3 constants ----------------------------------------------------

# The merge set. `exact` fuses both ways; generalization/special_case attach ONE
# way to a SINGLE best target; every other match_kind is a synapse.
FUSE = "exact"
ATTACH = ("generalization", "special_case")
MERGE_KINDS = frozenset((FUSE,) + ATTACH)
CELL_MAX_ORGANS = 48  # C3 guard: exceeding this means the merge rule broke

# Rule 2 ranking: confidence, then generalization before special_case, then id.
# ATTACH_RANK must cover every kind `--attach` accepts, or the ranking KeyErrors.
CONF_RANK = {"high": 0, "medium": 1, "low": 2}
ATTACH_RANK = {"generalization": 0, "special_case": 1, "invocation": 2, "related": 3}
ATTACHABLE = tuple(ATTACH_RANK)  # what --attach may name (default stays ATTACH)

# Weak bonds -> synapses. `formalizes` with match_kind invocation/related joins
# them (rule 3); `contains` is containment (supercells), never a synapse.
WEAK_EDGE_KINDS = frozenset(("depends", "mentions", "relates", "cites", "links"))

TRACE_CAP = 64  # safety valve; `truncated` records what a cap dropped (never silent)

# SCHEMA v2 facet bit 8 = "this node IS an external page". A cell is never an ext
# node, so the bit is masked off when a page organ's facets fold into its cell (the
# DB bits 9+ ARE kept: "this atom has an nLab page" is a useful node-level filter).
F_EXT = 1 << 8


# ---- small helpers ----------------------------------------------------------

def _iter_jsonl(path: Path, *, skip_meta: bool = True):
    """Yield rows of a brain JSONL, skipping the leading `_meta` line."""
    if not path.exists():
        return
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if skip_meta and "_meta" in row and len(row) == 1:
                continue
            yield row


def _bare(decl_id: str) -> str:
    """`decl:Mathlib:Foo.bar` -> `Foo.bar`."""
    parts = decl_id.split(":", 2)
    return parts[2] if len(parts) == 3 else decl_id


class Prov:
    """Interns provenance dicts so organs/traces carry a small int index.

    The table ships in the file's `_meta.prov`; `prov` on an organ or trace is an
    index into it. This is what makes a queued (AI) tag provenance-distinguishable
    from a merged `@[wikidata]` one (acceptance C7) without repeating the dict on
    every row.
    """

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self.table: list[dict] = []

    def intern(self, prov: dict | None) -> int:
        p = prov or {}
        key = json.dumps(p, sort_keys=True)
        if key not in self._index:
            self._index[key] = len(self.table)
            self.table.append(p)
        return self._index[key]


class DSU:
    """Union-find. Roots are chosen by lowest id so output is deterministic."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        p = self.parent
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if rb < ra:  # deterministic: the smaller id becomes the root
            ra, rb = rb, ra
        self.parent[rb] = ra
        return ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


# ---- inputs -----------------------------------------------------------------

def load_overrides(path: Path = OVERRIDES_IN) -> dict[str, dict]:
    """`grounding_overrides.jsonl` -> {qid: {"match_kind:<Decl>": kind, ...}}.

    These point-fixes are applied upstream by `catalog/build_graph_v2.py`, but that
    is a heavy rebuild; applying the same file here too makes a curated fix effective
    on the next cell build and is idempotent once upstream catches up.
    """
    out: dict[str, dict] = {}
    for row in _iter_jsonl(path, skip_meta=False):
        qid = row.get("qid")
        if qid and isinstance(row.get("set"), dict):
            out.setdefault(qid, {}).update(row["set"])
    return out


def load_rejected() -> set[tuple[str, str]]:
    """(qid, bare decl) claims a reviewer or skeptic REJECTED — never bond (C7).

    Two sources: the LLM triage's `cut` decisions (`bot/state/cut_log.json`) and the
    discovery pipeline's skeptic rejections (`brain/data/discovery_rejected.jsonl`).
    """
    rejected: set[tuple[str, str]] = set()
    cut_log = BOT_STATE / "cut_log.json"
    if cut_log.exists():
        try:
            for row in json.loads(cut_log.read_text()):
                qid = row.get("qid")
                triage = row.get("triage") or {}
                decl = triage.get("suggested_decl") or row.get("decl")
                if qid and decl:
                    rejected.add((qid, decl))
        except Exception as exc:  # fail-soft: a malformed cut log must not break the build
            print(f"  ! cut_log unreadable ({exc}) — continuing", file=sys.stderr)
    for row in _iter_jsonl(REJECTED_IN, skip_meta=False):
        if row.get("verdict") == "reject" and row.get("qid") and row.get("decl"):
            rejected.add((row["qid"], row["decl"]))
    return rejected


def load_tag_queue() -> list[dict]:
    """The `@[wikidata]` tag queue, read LOCALLY from `bot/state/` — no network.

    Mirrors the assembly of `bot/publish_queue.py` (which POSTs the same rows to
    /api/queue). A queue tag makes the SAME claim as a merged `@[wikidata]`
    attribute, but AI-generated: it is a strong bond whose provenance must stay
    distinguishable (SCHEMA "Strong-bond sources", C7).

    Fail-soft by contract: a missing/!unreadable queue yields NO queue organs and
    never a hard error.
    """
    items: list[dict] = []

    def _read(name: str) -> list:
        path = BOT_STATE / name
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            print(f"  ! {name} unreadable ({exc}) — skipping", file=sys.stderr)
            return []
        if isinstance(data, dict):
            return data.get("items", [])
        return data if isinstance(data, list) else []

    for row in _read("seed_queue.json"):
        # A seed item's (qid, decl) is ALREADY the corrected claim; any reject/revise
        # note refers to the superseded decl, not this one.
        items.append({"qid": row.get("qid"), "decl": row.get("decl"),
                      "status": row.get("status", "unreviewed"), "src": "seed_queue"})
    for row in _read("recycle_queue.json"):
        triage = row.get("triage") or {}
        qid = triage.get("suggested_qid") or row.get("qid")
        decl = triage.get("suggested_decl") or row.get("decl") or row.get("current_decl")
        items.append({"qid": qid, "decl": decl, "status": "recycled",
                      "orig_qid": row.get("qid"), "src": "recycle_queue"})
    for row in _read("brain_queue.json"):
        items.append({"qid": row.get("qid"), "decl": row.get("decl"),
                      "status": "brain", "src": "brain_queue"})
    for row in _read("pool_candidates.json"):
        items.append({"qid": row.get("qid"), "decl": row.get("decl"),
                      "status": row.get("status", "unreviewed"), "src": "pool_candidates"})
    return [i for i in items if i.get("qid") and i.get("decl")]


def queue_bonds(nodes: dict[str, dict], rejected: set[tuple[str, str]],
                stats: Counter) -> list[dict]:
    """Resolve queue items to (qid, decl node id) strong bonds.

    Drops: rejected claims (C7), unknown QIDs, and decls that do not resolve to a
    decl node (e.g. a stale name like `Basis` after the `Module.Basis` rename).
    """
    by_bare: dict[str, str] = {}
    for nid, node in nodes.items():
        if node["type"] == "decl":
            by_bare.setdefault(node["label"], nid)

    bonds: list[dict] = []
    for item in load_tag_queue():
        qid, decl = item["qid"], item["decl"]
        if (qid, decl) in rejected:
            stats["queue_rejected"] += 1
            continue
        # A retargeted item supersedes its original claim: that (orig_qid, decl)
        # pair was rejected by a reviewer and must never bond.
        if item.get("orig_qid") and item["orig_qid"] != qid:
            rejected.add((item["orig_qid"], decl))
        if qid not in nodes or nodes[qid]["type"] != "concept":
            stats["queue_unknown_qid"] += 1
            continue
        decl_id = by_bare.get(decl)
        if not decl_id:
            stats["queue_unresolved_decl"] += 1
            continue
        stats["queue_bonded"] += 1
        bonds.append({"qid": qid, "decl": decl_id, "status": item["status"],
                      "src": item["src"]})
    return bonds


# ---- the merge function (SCHEMA rules 1-5) ----------------------------------

def merge(nodes: dict[str, dict], formalizes: list[dict], qbonds: list[dict],
          stats: Counter, attach_kinds: tuple[str, ...] = ATTACH
          ) -> tuple[DSU, dict[str, list], dict[str, list]]:
    """Fuse organs into cells. A FUNCTION — chaining is structurally impossible.

    Returns (dsu, exact_by_qid, attach_by_qid). Container-targeted (`field`) edges
    are NOT merged here: rule 5 routes them to supercell organs.
    """
    dsu = DSU()
    exact_by_qid: dict[str, list] = defaultdict(list)
    attach_by_qid: dict[str, list] = defaultdict(list)

    for edge in formalizes:
        kind = (edge.get("evidence") or {}).get("match_kind")
        src, dst = edge["src"], edge["dst"]
        if dst.startswith("path:"):
            continue  # rule 5 — supercell organ, never a cell merge
        if kind == FUSE:
            exact_by_qid[src].append(edge)
        elif kind in attach_kinds:
            attach_by_qid[src].append(edge)
        # rule 3: invocation/related fall through to synapses

    # A queue tag is the same claim as `@[wikidata]` -> treat as `exact` (rule 1),
    # but keep its own provenance so C7 can tell the two apart.
    for bond in qbonds:
        exact_by_qid[bond["qid"]].append({
            "src": bond["qid"], "dst": bond["decl"], "kind": "formalizes",
            "confidence": "medium",
            "provenance": {"source": "tag-queue",
                           "method": f"AI-queued @[wikidata] candidate ({bond['status']})",
                           "queue": bond["src"]},
            "evidence": {"match_kind": FUSE, "queued": True, "status": bond["status"]},
        })

    # Every (concept, decl) pair actually used for a merge. Any formalizes edge NOT
    # in here stays a synapse — including an attach edge skipped because the concept
    # already had an exact home, which would otherwise be dropped on the floor.
    merged_pairs: set[tuple[str, str]] = set()

    # Rule 1 — `exact` fuses BOTH ways. A concept fuses all of its exact decls; a
    # decl fuses every concept that exact-formalizes it (the Module case).
    for qid, edges in exact_by_qid.items():
        for edge in edges:
            dsu.union(qid, edge["dst"])
            merged_pairs.add((qid, edge["dst"]))
            stats["fuse_exact"] += 1

    # Rule 2 — a concept with NO exact decl of its own (no formal home) attaches to
    # its SINGLE best generalization/special_case target. One target => it can never
    # bridge two cells, which is precisely what a transitive closure gets wrong.
    for qid in sorted(attach_by_qid):
        if qid in exact_by_qid:
            stats["attach_skipped_has_exact"] += 1
            continue
        best = min(attach_by_qid[qid], key=lambda e: (
            CONF_RANK.get(e.get("confidence"), 3),
            ATTACH_RANK[(e.get("evidence") or {})["match_kind"]],
            e["dst"],
        ))
        dsu.union(qid, best["dst"])
        merged_pairs.add((qid, best["dst"]))
        stats["attach_single_best"] += 1

    return dsu, exact_by_qid, attach_by_qid, merged_pairs


def apply_overrides(formalizes: list[dict], overrides: dict[str, dict],
                    stats: Counter) -> None:
    """Re-label curated match_kinds in place (`match_kind:<bare decl>` keys)."""
    for edge in formalizes:
        setter = overrides.get(edge["src"])
        if not setter or not edge["dst"].startswith("decl:"):
            continue
        want = setter.get("match_kind:" + _bare(edge["dst"]))
        evidence = edge.setdefault("evidence", {})
        if want and evidence.get("match_kind") != want:
            evidence["match_kind"] = want
            evidence["override"] = True
            stats["override_applied"] += 1


# ---- cell assembly ----------------------------------------------------------

def anchor_of(members: list[str], exact_concepts: set[str]) -> str:
    """The cell's canonical organ — it NAMES the atom, so it must be the concept the
    atom actually is.

    An `exact` concept wins over one merely absorbed by rule 2: a plain "lowest QID"
    rule names the Euclidean-space atom "plane" (Q17285 < Q17295) and the polygon
    atom "Quadrilateral", because a generalization/special_case organ can easily
    carry a lower QID than the exact match. Ties break on lowest QID, so the choice
    stays deterministic.
    """
    qids = [m for m in members if m.startswith("Q") and m[1:].isdigit()]
    exact = [q for q in qids if q in exact_concepts]
    pool = exact or qids
    if pool:
        return min(pool, key=lambda q: int(q[1:]))
    decls = sorted(m for m in members if m.startswith("decl:"))
    return decls[0] if decls else min(members)


def build_cells(nodes: dict[str, dict], edges_by_kind: dict[str, list],
                qbonds: list[dict], stats: Counter, prov: Prov,
                attach_kinds: tuple[str, ...] = ATTACH
                ) -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    """Assemble cells, supercell organs, and the organ->cell owner map."""
    formalizes = edges_by_kind["formalizes"]
    dsu, exact_by_qid, attach_by_qid, merged_pairs = merge(
        nodes, formalizes, qbonds, stats, attach_kinds)

    # Bond + provenance for every merged organ, so the cell card can say WHY the
    # organ is here (and C7 can separate queued from merged tags).
    organ_meta: dict[tuple[str, str], tuple[str, int]] = {}  # (root, organ) -> (bond, prov)

    def prov_rank(edge: dict) -> tuple:
        """Strongest evidence first; ties broken on ids so the pick is deterministic.

        An organ fused by BOTH a merged `@[wikidata]` attribute and an AI queue
        candidate must show the MERGED provenance. Last-write-wins let the synthetic
        queue bond (appended after the real edges) overwrite it, inverting C7 on 12
        shipped organs — the queue's entire contract is that it stays distinguishable
        from, and weaker than, a tag that actually landed in Mathlib.
        """
        p = edge.get("provenance") or {}
        method = p.get("method") or ""
        if p.get("source") == "tag-queue":
            tier = 2
        else:
            tier = 0 if "@[wikidata] attribute" in method else 1
        return (tier, edge["src"], edge["dst"])

    best: dict[tuple[str, str], dict] = {}

    def offer(key: tuple[str, str], edge: dict) -> None:
        cur = best.get(key)
        if cur is None or prov_rank(edge) < prov_rank(cur):
            best[key] = edge

    for qid, edge_list in exact_by_qid.items():
        for edge in edge_list:
            offer((dsu.find(qid), qid), edge)
            offer((dsu.find(edge["dst"]), edge["dst"]), edge)
    for key, edge in best.items():
        organ_meta[key] = (FUSE, prov.intern(edge.get("provenance")))
    for qid, edge_list in attach_by_qid.items():
        if qid in exact_by_qid:
            continue
        best = min(edge_list, key=lambda e: (
            CONF_RANK.get(e.get("confidence"), 3),
            ATTACH_RANK[(e.get("evidence") or {})["match_kind"]], e["dst"]))
        pidx = prov.intern(best.get("provenance"))
        organ_meta[(dsu.find(qid), qid)] = (
            (best.get("evidence") or {})["match_kind"], pidx)
        # The attach target itself needs a bond too: in a cell formed ONLY by rule 2
        # the decl would otherwise carry no bond at all and the card could not say
        # why it is here.
        organ_meta.setdefault((dsu.find(best["dst"]), best["dst"]), ("formalizes", pidx))

    groups = dsu.groups()

    # Rule 5 — `field` / concept->container: the concept is an organ of the
    # SUPERCELL, **never a cell** ("Linear algebra" is the LinearAlgebra folder, not
    # the `Module` atom, and not an atom of its own — searching it should land on the
    # folder). A concept that ALSO merged into a cell keeps the cell as its owner and
    # is merely listed on the supercell too.
    supercell_organs: dict[str, list] = defaultdict(list)
    field_only: set[str] = set()
    for edge in formalizes:
        if not edge["dst"].startswith("path:"):
            continue
        supercell_organs[edge["dst"]].append({
            "kind": "concept", "id": edge["src"],
            "label": (nodes.get(edge["src"]) or {}).get("label"),
            "bond": (edge.get("evidence") or {}).get("match_kind", "field"),
            "prov": prov.intern(edge.get("provenance")),
        })
        stats["supercell_concept_organ"] += 1
        if edge["src"] not in dsu.parent:
            # Its only home is the supercell. It owns no cell — but it keeps its
            # edges: they now hang off the SUPERCELL (see owner[] below). These are
            # field-level hubs ("Linear algebra", "manifold"), so dropping their
            # relates/mentions/links instead would cost ~10.8k synapses.
            field_only.add(edge["src"])

    # Lone particles: a concept or decl that merged with nothing is still an atom —
    # unless rule 5 already gave it a supercell home.
    for nid, node in nodes.items():
        if node["type"] in ("concept", "decl") and nid not in dsu.parent:
            if nid in field_only:
                stats["field_concept_no_cell"] += 1
                continue
            groups[nid] = [nid]

    # ---- cells
    cells: dict[str, dict] = {}
    owner: dict[str, str] = {}  # organ id -> cell id (the aliases.json backbone)
    parent = {e["dst"]: e["src"] for e in edges_by_kind["contains"]}

    exact_concepts = set(exact_by_qid)
    for root, members in groups.items():
        members = sorted(members)
        anchor = anchor_of(members, exact_concepts)
        cid = "cell:" + anchor
        organs, supercells, facets = [], [], 0
        for mid in members:
            node = nodes.get(mid)
            if node is None:
                continue
            bond, pidx = organ_meta.get((root, mid), (None, None))
            kind = {"concept": "concept", "decl": "decl"}.get(node["type"], node["type"])
            organ = {"kind": kind, "id": mid, "label": node.get("label")}
            if bond:
                organ["bond"] = bond
            if pidx is not None:
                organ["prov"] = pidx
            organs.append(organ)
            owner[mid] = cid
            facets |= node.get("f", 0)
            if node["type"] == "decl":
                container = parent.get(mid)
                if container and container not in supercells:
                    supercells.append(container)
        if not organs:
            continue
        label = (nodes.get(anchor) or {}).get("label") or anchor
        cell = {"id": cid, "anchor": anchor, "label": label, "organs": organs}
        if supercells:
            cell["supercells"] = sorted(supercells)
        if facets:
            cell["f"] = facets
        cells[cid] = cell

    # A rule-5 field concept owns no cell, but its bonds are real: route them to the
    # supercell that DOES hold it, so a synapse may legitimately land on a module
    # ("this atom relates to the whole of LinearAlgebra"). v2 already drew
    # container-level rollup edges between bubbles, so this is a shape the renderer
    # understands.
    for path, organs in supercell_organs.items():
        for organ in organs:
            if organ["kind"] == "concept" and organ["id"] in field_only:
                owner[organ["id"]] = path

    return cells, owner, supercell_organs, merged_pairs


def attach_pages(cells: dict[str, dict], owner: dict[str, str], nodes: dict[str, dict],
                 xrefs: list[dict], supercell_organs: dict[str, list],
                 stats: Counter, prov: Prov) -> list[dict]:
    """Rule 4 — pages NEVER bridge.

    A page claimed by exactly one cell attaches as that cell's organ. A page claimed
    by >1 cell is an AREA page: it becomes an organ of the claimants' common module
    ancestor (a supercell) and emits a weak synapse between the claimants instead.
    Returns the co-claim synapse seeds.
    """
    claims: dict[str, list[dict]] = defaultdict(list)
    for edge in xrefs:
        cid = owner.get(edge["src"])
        page = edge["dst"]
        if cid and page in nodes:
            claims[page].append({"cell": cid, "edge": edge})

    co_claims: list[dict] = []
    for page, rows in sorted(claims.items()):
        claimants = sorted({r["cell"] for r in rows})
        node = nodes[page]
        if len(claimants) == 1 and claimants[0].startswith("path:"):
            # Its sole claimant is a rule-5 field concept, which lives on a supercell
            # — so its pages do too ("linear algebra" the nLab page belongs to the
            # LinearAlgebra folder, not to any atom inside it).
            supercell_organs[claimants[0]].append({
                "kind": "page", "id": page, "label": node.get("label"),
                "db": node.get("db"), "bond": "xref",
                "prov": prov.intern(rows[0]["edge"].get("provenance")),
            })
            owner[page] = claimants[0]
            stats["page_organ_supercell"] += 1
            continue

        if len(claimants) == 1:
            cid = claimants[0]
            cells[cid]["organs"].append({
                "kind": "page", "id": page, "label": node.get("label"),
                "db": node.get("db"), "bond": "xref",
                "prov": prov.intern(rows[0]["edge"].get("provenance")),
            })
            # Take the page's DB bits (bit9+: "this atom has an nLab/Stacks/... page"
            # — a useful node-level filter) but MASK OFF bit8 F_EXT. Bit 8 is the
            # node-type predicate "this node IS an external page"; a cell never is,
            # and OR-ing it set the bit on 1,868 non-ext cells.
            cells[cid]["f"] = cells[cid].get("f", 0) | (node.get("f", 0) & ~F_EXT)
            owner[page] = cid
            stats["page_organ"] += 1
            continue

        # multi-claimant -> supercell organ + weak synapses between the claimants
        stats["page_area"] += 1
        home = common_supercell(cells, claimants)
        if home:
            stats["page_area_homed"] += 1
        else:
            # No common ancestor (claimants span libraries, or none has a decl at
            # all). Without a fallback the page attaches NOWHERE — not a cell, not a
            # supercell — and vanishes from the graph. That silently swallowed 15 of
            # 123 area pages, DLMF 1.9 among them, which is the SCHEMA's own worked
            # example of rule 4. Fall back to the shallowest supercell any claimant
            # has (the library root), and only give up if no claimant has one.
            home = fallback_supercell(cells, claimants)
            stats["page_area_fallback" if home else "page_area_homeless"] += 1
        if home:
            supercell_organs[home].append({
                "kind": "page", "id": page, "label": node.get("label"),
                "db": node.get("db"), "bond": "area-page",
                "claimants": claimants,
                "prov": prov.intern(rows[0]["edge"].get("provenance")),
            })
        for i, a in enumerate(claimants):
            for b in claimants[i + 1:]:
                co_claims.append({
                    "src": a, "dst": b, "kind": "co-page",
                    "trace": {"kind": "co-page", "src": page, "dst": page,
                              "evidence": {"page": page, "db": node.get("db"),
                                           "label": node.get("label"),
                                           "note": "both cells cross-reference this page"},
                              "prov": prov.intern(rows[0]["edge"].get("provenance"))},
                })
    return co_claims


def _stake(cells: dict[str, dict], claimant: str) -> str | None:
    """The supercell a claimant stands in — itself, if it IS one (rule-5 concepts)."""
    if claimant.startswith("path:"):
        return claimant
    sups = (cells.get(claimant) or {}).get("supercells") or []
    # a cell may span modules: take its shallowest home as that cell's stake
    return min(sups, key=lambda s: (s.count("/"), s)) if sups else None


def common_supercell(cells: dict[str, dict], claimants: list[str]) -> str | None:
    """Deepest `path:` prefix shared by every claimant's supercell."""
    paths: list[list[str]] = []
    for cid in claimants:
        stake = _stake(cells, cid)
        if not stake:
            return None
        paths.append(stake.split("/"))
    shared: list[str] = []
    for parts in zip(*paths):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    return "/".join(shared) if len(shared) > 1 else None


def fallback_supercell(cells: dict[str, dict], claimants: list[str]) -> str | None:
    """Shallowest supercell any claimant has — the last stop before a page is lost.

    Reached when the claimants share no common ancestor (or a claimant is a
    concept-only cell with no decl and therefore no supercell at all). A library
    root is a coarse home, but a coarse home beats vanishing.
    """
    sups = sorted({s for cid in claimants
                   for s in ([cid] if cid.startswith("path:")
                             else (cells.get(cid) or {}).get("supercells") or [])})
    return min(sups, key=lambda s: (s.count("/"), s)) if sups else None


def attach_articles(cells: dict[str, dict], owner: dict[str, str],
                    nodes: dict[str, dict], stats: Counter, prov: Prov) -> None:
    """A WikiLean article about the object is a strong bond (organ attach)."""
    pidx = prov.intern({"source": "wikilean", "method": "annotated article (D1)"})
    for cid, cell in cells.items():
        for organ in list(cell["organs"]):
            if organ["kind"] != "concept":
                continue
            node = nodes.get(organ["id"]) or {}
            slug = node.get("slug")
            if not slug or not node.get("article_annotations"):
                continue
            cell["organs"].append({
                "kind": "article", "id": slug, "label": slug.replace("_", " "),
                "bond": "article", "prov": pidx,
                "annotations": node["article_annotations"],
            })
            owner.setdefault(slug, cid)
            stats["article_organ"] += 1


def attach_statements(cells: dict[str, dict], owner: dict[str, str],
                      nodes: dict[str, dict], matches: list[dict],
                      stats: Counter, prov: Prov) -> list[dict]:
    """TheoremGraph `matches` (arXiv statement <-> Mathlib theorem) — organ attach.

    Rule 4 generalises beyond pages: NO organ may bridge. TheoremGraph matches the
    same arXiv statement to decls living in different cells (219 of them), so a
    naive attach puts one organ in two cells and `aliases.json` stops being a
    function — which every API/MCP route depends on (acceptance C4).

    So: claimed by exactly one cell => organ. Claimed by >1 => it is EVIDENCE that
    those cells are related, not that either owns it => a synapse whose trace names
    the shared statement.
    """
    claims: dict[str, list[dict]] = defaultdict(list)
    for edge in matches:
        cid = owner.get(edge["src"])
        if cid and cid in cells:
            claims[edge["dst"]].append({"cell": cid, "edge": edge})

    shared: list[dict] = []
    for sid, rows in sorted(claims.items()):
        claimants = sorted({r["cell"] for r in rows})
        node = nodes.get(sid) or {}
        if len(claimants) == 1:
            cid = claimants[0]
            cells[cid]["organs"].append({
                "kind": "statement", "id": sid, "label": node.get("label"),
                "bond": "matches", "prov": prov.intern(rows[0]["edge"].get("provenance")),
            })
            owner.setdefault(sid, cid)
            stats["statement_organ"] += 1
            continue
        stats["statement_shared"] += 1
        for i, a in enumerate(claimants):
            for b in claimants[i + 1:]:
                shared.append({
                    "src": a, "dst": b, "kind": "co-statement",
                    "trace": {"kind": "co-statement", "src": sid, "dst": sid,
                              "evidence": {"statement": sid, "label": node.get("label"),
                                           "note": "both cells are matched to this arXiv "
                                                   "statement by TheoremGraph"},
                              "prov": prov.intern(rows[0]["edge"].get("provenance"))},
                })
    return shared


# ---- synapses ---------------------------------------------------------------

def build_synapses(cells: dict[str, dict], owner: dict[str, str],
                   edges_by_kind: dict[str, list], co_claims: list[dict],
                   merged_pairs: set[tuple[str, str]],
                   stats: Counter, prov: Prov, *,
                   supercells: set[str] | None = None,
                   links_path: Path = EDGES_LINKS_IN) -> list[dict]:
    """Aggregate every weak bond into ONE synapse per endpoint pair, keeping traces.

    Both endpoints must resolve to an ATOM — a cell, or a supercell for a rule-5
    field concept (which owns no cell but keeps its bonds). That resolution IS the
    v3 projection: unanchored frontier pages own nothing, so their links drop out
    (docs/BRAIN-V3.md "Dropped in v3") — they carried no concept-level connectivity
    anyway.
    """
    agg: dict[tuple[str, str], dict] = {}
    valid = set(cells) | (supercells or set())

    def add(a: str, b: str, kind: str, trace: dict) -> None:
        # Counted HERE, not at the call sites: an edge whose endpoints land in the
        # same cell is an INTRA-cell bond and is dropped, so counting before this
        # guard over-reports every weak kind.
        if a == b:
            stats[f"intracell_{kind}"] += 1
            return
        if a not in valid or b not in valid:
            stats[f"unowned_{kind}"] += 1
            return
        stats[f"synapse_{kind}"] += 1
        key = (a, b) if a < b else (b, a)
        row = agg.get(key)
        if row is None:
            row = agg[key] = {"src": key[0], "dst": key[1], "weight": 0,
                              "kinds": Counter(), "traces": [], "truncated": 0}
        row["weight"] += 1
        row["kinds"][kind] += 1
        if len(row["traces"]) < TRACE_CAP:
            row["traces"].append(trace)
        else:
            row["truncated"] += 1

    def trace_of(edge: dict, kind: str) -> dict:
        trace = {"kind": kind, "src": edge["src"], "dst": edge["dst"],
                 "prov": prov.intern(edge.get("provenance"))}
        if edge.get("evidence"):
            trace["evidence"] = edge["evidence"]
        return trace

    # in-memory weak kinds
    for kind in ("depends", "mentions", "relates", "cites"):
        for edge in edges_by_kind[kind]:
            a, b = owner.get(edge["src"]), owner.get(edge["dst"])
            if a and b:
                add(a, b, kind, trace_of(edge, kind))

    # Rule 3 — invocation/related are synapses, never merges. This also catches any
    # gen/special_case edge that did NOT produce a merge (the concept already had an
    # exact home, or the kind is not in the merge set): the relationship is real and
    # is kept as a synapse rather than dropped.
    for edge in edges_by_kind["formalizes"]:
        mk = (edge.get("evidence") or {}).get("match_kind")
        if edge["dst"].startswith("path:") or (edge["src"], edge["dst"]) in merged_pairs:
            continue
        a, b = owner.get(edge["src"]), owner.get(edge["dst"])
        if a and b:
            add(a, b, mk or "formalizes", trace_of(edge, mk or "formalizes"))

    # rule 4 — co-claimed area pages
    for row in co_claims:
        add(row["src"], row["dst"], row["kind"], row["trace"])

    # `links` streams: 630k rows / 146MB. This is the payoff of ingesting each
    # database's INTERNAL links — a page-to-page link becomes a cell-to-cell synapse
    # whose trace names both pages.
    #
    # edges_links.jsonl carries the same fact in TWO forms: 618k raw ext->ext page
    # links, and 11,540 concept->concept rows that build_edges already projected from
    # those same links (evidence.projected, naming src_page/dst_page). Consuming both
    # blindly double-counts every pre-projected link — two traces and weight 2 for one
    # nLab hyperlink. But dropping the projected rows loses MORE than it saves: a page
    # claimed by several cells is an area page and owns no cell (rule 4), so its links
    # cannot project through ownership — and area pages are precisely the hubs that
    # carry the most links. Raw-only measured 6,415 bonds vs 11,540 pre-projected.
    #
    # So: keep both, deduplicated at the FACT level. Raw links go first (their trace
    # names both pages, which is what the evidence drawer shows); a projected row is
    # then used only if its underlying page pair was not already consumed.
    if not links_path.exists():
        # LOUD: this file is gitignored (146MB), so a fresh clone silently loses every
        # external-DB link synapse — the whole point of ingesting internal links.
        print(f"  ! {links_path.name} absent — NO link synapses will be built "
              f"(rebuild: python3 brain/build_edges.py)", file=sys.stderr)
        stats["links_file_missing"] = 1

    consumed: set[tuple[str, str]] = set()
    projected: list[dict] = []
    for edge in _iter_jsonl(links_path):
        ev = edge.get("evidence") or {}
        if ev.get("projected"):
            projected.append(edge)       # 11,540 rows — buffered, resolved below
            continue
        a, b = owner.get(edge["src"]), owner.get(edge["dst"])
        if a and b:
            add(a, b, "links", trace_of(edge, "links"))
            consumed.add((edge["src"], edge["dst"]))

    for edge in projected:
        ev = edge["evidence"]
        via = ev.get("via")
        pair = (f"xref:{via}:{ev.get('src_page')}", f"xref:{via}:{ev.get('dst_page')}")
        if pair in consumed:
            stats["links_preprojected_deduped"] += 1
            continue
        a, b = owner.get(edge["src"]), owner.get(edge["dst"])
        if a and b:
            add(a, b, "links", trace_of(edge, "links"))
            stats["links_preprojected_kept"] += 1

    out = []
    for (a, b), row in sorted(agg.items()):
        row["kinds"] = dict(sorted(row["kinds"].items()))
        if not row["truncated"]:
            row.pop("truncated")
        out.append(row)
    return out


# ---- tagger-quality diagnostic ----------------------------------------------

def cell_review(cells: dict[str, dict], nodes: dict[str, dict],
                stats: Counter, *, min_absorbed: int = 2) -> list[dict]:
    """Flag cells that ballooned via rule 2 — a tagger-quality worklist.

    Jack's rule (2026-07-17): a cell that balloons is not a merge-rule failure, it
    means the AI taggers mis-graded a `related`/`invocation` as
    `generalization`/`special_case`. So rule 2 stays exactly as contracted and the
    SIZE becomes the signal: a decl absorbing several home-less concepts is the
    shape of a bad grade.

    Each row names the exact claim to re-grade and is shaped to drop straight into
    `catalog/data/grounding_overrides.jsonl` (see the Q13471665 "Vector" fix, which
    this diagnostic is the generalisation of). Feeds the tag-quality loop.
    """
    review: list[dict] = []
    for cid, cell in cells.items():
        concepts = [o for o in cell["organs"] if o["kind"] == "concept"]
        decls = [o["id"] for o in cell["organs"] if o["kind"] == "decl"]
        absorbed = [o for o in concepts if o.get("bond") in ATTACH]

        # Rule-2 balloon: one decl absorbing several home-less concepts.
        rule = "rule2-absorption" if len(absorbed) >= min_absorbed else None

        # Rule-1 weld: `exact` fuses BOTH ways, so it is transitive by design (that
        # is what puts riemannZeta AND completedRiemannZeta in one atom). The cost is
        # that ONE over-broad exact grade welds every decl it names, plus every
        # concept naming those, into a single atom — e.g. the survey concept
        # "Bijection, injection and surjection" exact-claiming Function.{Bijective,
        # Injective,Surjective} drags Bijection and Surjective function into one cell,
        # though no edge joins them. Scoping this worklist to rule 2 left exactly that
        # case invisible — the only chaining that actually occurs. Same doctrine, same
        # channel: surface the grade, do not bend the rule.
        exact_concepts = [o for o in concepts if o.get("bond") == FUSE]
        if len(exact_concepts) >= 2 and len(decls) >= 2:
            rule = "rule1-exact-weld"

        if rule is None:
            continue
        suspects = absorbed if rule == "rule2-absorption" else exact_concepts
        note = ("each suspect claim asserts this concept has no formal home of its own "
                "and belongs in this atom; if that is wrong the grade should be "
                "`related` or `invocation`"
                if rule == "rule2-absorption" else
                "several concepts `exact`-claim several decls here, welding them into "
                "one atom; `exact` means IDENTITY, so an over-broad survey concept "
                "claiming many decls is the likely mis-grade")
        review.append({
            "cell": cid,
            "label": cell["label"],
            "rule": rule,
            "n_organs": len(cell["organs"]),
            "n_absorbed": len(suspects),
            "decls": decls,
            "suspect_claims": [
                {"qid": o["id"], "label": o.get("label"), "match_kind": o.get("bond"),
                 "absorbed_into": decls[0] if decls else None}
                for o in suspects
            ],
            "note": note + " — fix via catalog/data/grounding_overrides.jsonl",
        })
    review.sort(key=lambda r: (-r["n_absorbed"], -r["n_organs"], r["cell"]))
    stats["cells_flagged_rule1"] = sum(1 for r in review if r["rule"] == "rule1-exact-weld")
    stats["cells_flagged_for_review"] = len(review)
    return review


# ---- output -----------------------------------------------------------------

def write_jsonl(path: Path, meta: dict, rows: list[dict]) -> None:
    with path.open("w") as fh:
        fh.write(json.dumps({"_meta": meta}, separators=(",", ":")) + "\n")
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def build(*, do_layout: bool = True, attach_kinds: tuple[str, ...] = ATTACH,
          links_path: Path = EDGES_LINKS_IN) -> tuple[list[dict], list[dict], dict]:
    stats: Counter = Counter()
    prov = Prov()

    print("reading organ layer…", file=sys.stderr)
    nodes = {n["id"]: n for n in _iter_jsonl(NODES_IN)}
    edges_by_kind: dict[str, list] = defaultdict(list)
    for edge in _iter_jsonl(EDGES_IN):
        edges_by_kind[edge["kind"]].append(edge)
    print(f"  {len(nodes)} organs, "
          f"{sum(len(v) for v in edges_by_kind.values())} edges", file=sys.stderr)

    apply_overrides(edges_by_kind["formalizes"], load_overrides(), stats)
    rejected = load_rejected()
    qbonds = queue_bonds(nodes, rejected, stats)
    print(f"  tag queue: {stats['queue_bonded']} bonded, "
          f"{stats['queue_rejected']} rejected, "
          f"{stats['queue_unresolved_decl']} unresolved", file=sys.stderr)

    print("merging organs into cells…", file=sys.stderr)
    cells, owner, supercell_organs, merged_pairs = build_cells(
        nodes, edges_by_kind, qbonds, stats, prov, attach_kinds)
    co_claims = attach_pages(cells, owner, nodes, edges_by_kind["xref"],
                             supercell_organs, stats, prov)
    attach_articles(cells, owner, nodes, stats, prov)
    co_claims += attach_statements(cells, owner, nodes, edges_by_kind["matches"],
                                   stats, prov)
    print(f"  {len(cells)} cells", file=sys.stderr)

    print("aggregating synapses…", file=sys.stderr)
    synapses = build_synapses(cells, owner, edges_by_kind, co_claims, merged_pairs,
                              stats, prov, supercells=set(supercell_organs),
                              links_path=links_path)
    print(f"  {len(synapses)} synapses", file=sys.stderr)

    review = cell_review(cells, nodes, stats)
    print(f"  {len(review)} cells flagged for tagger review", file=sys.stderr)

    if do_layout:
        from layout import layout_cells  # local import: numpy only needed for layout
        print("laying out (build-time force sim)…", file=sys.stderr)
        layout_cells(cells, synapses)

    rows = [cells[k] for k in sorted(cells)]
    meta = {
        "schema": "brain/SCHEMA.md#v3",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prov": prov.table,
        "supercell_organs": {k: v for k, v in sorted(supercell_organs.items())},
        "counts": {
            "cells": len(rows),
            "synapses": len(synapses),
            "organs": sum(len(c["organs"]) for c in rows),
            "multi_organ_cells": sum(1 for c in rows if len(c["organs"]) > 1),
            "largest_cell": max((len(c["organs"]) for c in rows), default=0),
        },
        "stats": dict(sorted(stats.items())),
    }
    return rows, synapses, meta, review


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-layout", action="store_true",
                    help="skip the build-time force sim (fast iteration)")
    ap.add_argument("--stats", action="store_true", help="print the stats table and exit")
    ap.add_argument("--attach", default=",".join(ATTACH),
                    help="match_kinds that may attach a home-less concept (SCHEMA rule 2); "
                         "the excluded ones become synapses instead. "
                         f"choose from: {', '.join(ATTACHABLE)}")
    args = ap.parse_args()

    # Validate: an unknown kind used to be accepted silently (and quietly build a
    # DIFFERENT graph), while a real-but-unranked kind raised a bare KeyError deep
    # in merge(). Widening the merge set is a contract change — fail loudly here.
    attach_kinds = tuple(k for k in args.attach.split(",") if k)
    unknown = [k for k in attach_kinds if k not in ATTACH_RANK]
    if unknown:
        ap.error(f"--attach: unknown match_kind(s) {unknown}; "
                 f"choose from {list(ATTACHABLE)}")
    cells, synapses, meta, review = build(do_layout=not args.no_layout,
                                          attach_kinds=attach_kinds)
    if args.stats:
        print(json.dumps({"counts": meta["counts"], "stats": meta["stats"]}, indent=1))
        return

    write_jsonl(CELLS_OUT, meta, cells)
    write_jsonl(SYNAPSES_OUT, {k: meta[k] for k in ("schema", "generated_at", "prov")}
                | {"counts": meta["counts"]}, synapses)
    write_jsonl(REVIEW_OUT, {"schema": "brain/SCHEMA.md#v3",
                             "generated_at": meta["generated_at"],
                             "note": "cells that ballooned via SCHEMA rule 2 — suspect "
                                     "AI tagger grades; fix via grounding_overrides.jsonl",
                             "counts": {"flagged": len(review)}}, review)
    print(json.dumps(meta["counts"], indent=1), file=sys.stderr)
    print(f"wrote {CELLS_OUT.relative_to(ROOT)} + {SYNAPSES_OUT.relative_to(ROOT)}"
          f" + {REVIEW_OUT.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
