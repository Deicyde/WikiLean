#!/usr/bin/env python3
"""Build per-CELL shards — SCHEMA v3's locality law as static JSON.

Reads `brain/data/{cells,synapses}.jsonl` (the atom layer) plus `nodes.jsonl` and
the `contains` edges (for organ payloads and the containment tree) and writes
`site/assets/brain/cells/`:

  manifest.json    scheme + supercell roots + prov table + shard directory
  <key>.json       prefix-named shards: {cell id -> entry}
  aliases.json     EVERY organ id -> its cell id  (the v2->v3 compat layer)
  labels.json      searchable cell rows (label + every organ label as an alias)
  supercells.json  the containment tree, whose leaves are now CELLS — plus the
                   FRONTIER areas (`frontier:<Area>` rows from
                   brain/data/frontier.jsonl, brain/build_frontier.py): each
                   lists the homeless cells assigned to it, so the client's
                   derived "no formal home" bucket drains to the handful of
                   genuinely unplaceable cells; each also carries its `prox`
                   formal-proximity arrays (PROXIMITY CONTRACT: per-cell
                   score/radius/provenance, parallel to `cells`) VERBATIM
  explorer.json    the whole flat graph: cells with build-time xy + synapses
  frontier_graph.json  brain/data/frontier_graph.json byte-copied VERBATIM — the
                   lazily-fetched client-side re-scoring input for the
                   Libraries toggle (FRONTIER GRAPH contract,
                   brain/build_frontier.py + brain/SCHEMA.md): cells /
                   exact-root-set formal weights / weighted frontier<->frontier
                   edges, so the shipped graph is exactly the one
                   test_frontier.py proved the parity law against
  traces/<key>.json  the LAZY trace sidecar: evidence for every supercell-involving
                   synapse (cell<->path and path<->path — the rows supercells.json
                   ships traceless), bucketed by the same longest-prefix scheme
                   applied to the pair key "<src>|<dst>"; _meta = manifest `traces`

Sharding is `build_shards.py`'s scheme, reused verbatim (longest-prefix keys, split
at MAX_SHARD_BYTES): the client loads manifest.json once and any cell is then ONE
fetch away — and that one fetch carries the entire cell card, because organ payloads
(Lean docstring + code, Wikidata description, licensed DB snippets, article
annotation counts) are embedded rather than referenced.

v3 vs v2: the v2 tree shards 73,318 nodes into 333MB. Cells shard 8,982 atoms —
the ~49k external pages are organs INSIDE cells now, not nodes — and `explorer.json`
carries the complete cell graph in one ~4MB file with positions already computed,
so the explorer renders without simulating anything.

Run: python3 brain/build_cell_shards.py   (after brain/build_cells.py)
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

from build_shards import (MAX_KEY_LEN, MAX_SHARD_BYTES, MIN_KEY_LEN, PAD,  # noqa: F401
                          shard_key)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRAIN_DATA = HERE / "data"
OUT_DIR = ROOT / "site" / "assets" / "brain" / "cells"
SCRATCH_DIR = ROOT / "site" / "assets"   # scratch swap dirs — OUTSIDE the copied tree

SYN_CAP = 200         # synapses kept per cell entry (every KIND first: pick_synapses)
SHARD_TRACE_CAP = 6   # traces kept per synapse IN THE SHARD (full set: query.py)
# traces per synapse in the LAZY trace sidecar (traces/<key>.json) — the evidence
# for supercell-involving synapses, which supercells.json deliberately ships
# traceless. 24 trims nothing on live data (max observed 19); per-row `tt` keeps
# the true total whenever it ever does.
SIDECAR_TRACE_CAP = 24
EXPLORER_BUDGET = 4_200_000
SNIPPET_CAP = 400     # chars of a licensed DB snippet carried into the card
AKA_CAP = 16          # search aliases per cell — statement titles yield first
# Organ kinds a human actually SEARCHES an atom by. A statement organ's label is an
# arXiv paper TITLE — the name of a document that mentions the atom, not a name for
# the atom — so it must never evict `Module.Dual` from the search index.
AKA_SEARCHABLE = ("concept", "decl", "article", "page")


def _iter(path: Path):
    if not path.exists():
        raise SystemExit(f"missing {path} — run python3 brain/build_cells.py first")
    with path.open() as fh:
        meta = json.loads(next(fh)).get("_meta", {})
        for line in fh:
            if line.strip():
                yield meta, json.loads(line)


def load_jsonl(path: Path) -> tuple[dict, list]:
    rows, meta = [], {}
    for meta, row in _iter(path):
        rows.append(row)
    return meta, rows


def organ_payload(organ: dict, nodes: dict[str, dict]) -> dict:
    """Embed what the cell card renders, so the card costs ONE fetch.

    This is the v3 answer to "clicking a concept should show the Lean code, the
    article, the Wikidata description, the LMFDB knowl, the Stacks description":
    every organ carries its own evidence, already licensed and trimmed.
    """
    node = nodes.get(organ["id"]) or {}
    out = dict(organ)
    kind = organ["kind"]
    if kind == "decl":
        for key in ("module", "decl_kind", "docstring", "code", "library",
                    "renamed_to"):
            if node.get(key):
                out[key] = node[key]
    elif kind == "concept":
        unit = node.get("unit") or {}
        if unit.get("description"):
            out["description"] = unit["description"]
        for key in ("slug", "article_annotations"):
            if node.get(key):
                out[key] = node[key]
        status = (node.get("display") or {}).get("status")
        if status:
            out["status"] = status
    elif kind == "page":
        for key in ("url", "kind_hint", "qid"):
            if node.get(key):
                out[key] = node[key]
        if node.get("snippet"):
            snippet = node["snippet"]
            if len(snippet) > SNIPPET_CAP:
                snippet = snippet[:SNIPPET_CAP].rsplit(" ", 1)[0] + "…"
            out["snippet"] = snippet
            # a snippet may never ship without its licence — per-source terms differ
            # and no-content sources carry ids+titles only (SCHEMA licences)
            out["snippet_license"] = node.get("snippet_license")
    elif kind == "statement":
        for key in ("arxiv_id", "ref", "license_open"):
            if node.get(key) is not None:
                out[key] = node[key]
    return out


def trim_trace(trace: dict) -> dict:
    """Shard-side trim: `depends` witness lists are unbounded; keep the first pair.

    Mirrors build_shards' rule — the shard is a rendering artifact, the full
    evidence stays in brain/data/synapses.jsonl (served by brain/query.py).
    """
    ev = trace.get("evidence")
    if ev and len(ev.get("witnesses") or []) > 1:
        trace = {**trace, "evidence": {**ev, "witnesses": ev["witnesses"][:1]}}
    return trace


def pick_traces(traces: list[dict], cap: int) -> list[dict]:
    """Choose a DIVERSE sample of traces — one per kind, round-robin — not the first N.

    A synapse's traces are grouped by kind, and `depends` outnumbers everything else
    by ~10:1. Taking the first `cap` therefore buries exactly the evidence that is
    worth reading: measured on the Algebra-over-a-field <-> Ring synapse, all 6 shown
    traces were `depends` while its one `links` trace — the cross-database page link,
    naming both pages — never rendered. Round-robin guarantees every KIND present in
    the synapse appears in the drawer before any kind repeats.
    """
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for t in traces:
        by_kind[t.get("kind") or "?"].append(t)
    out: list[dict] = []
    # rarest kind first: a lone `links` among 12 `depends` is the informative one
    order = sorted(by_kind, key=lambda k: (len(by_kind[k]), k))
    i = 0
    while len(out) < cap and any(by_kind.values()):
        progressed = False
        for kind in order:
            if by_kind[kind] and len(out) < cap:
                out.append(by_kind[kind].pop(0))
                progressed = True
        if not progressed:
            break
        i += 1
    return out


def pick_synapses(syns: list[dict], cap: int) -> list[dict]:
    """Choose WHICH synapses ship — every bond KIND represented before the cap is
    spent on more of the heaviest kind.

    The same disease pick_traces() cures, one level up. Sorting by `-w` and cutting
    at `cap` drops whole KINDS from a hub cell's card, because rare kinds are
    weight-1 BY NATURE: a single generalization/special_case/co-statement bond
    aggregates to weight 1, so ordering by weight GUARANTEES they sort last and are
    cut first (measured: every dropped synapse had weight 1-3). Measured on
    cell:Q17278 (Circle) — its one `generalization`, "a circle is a 2-sphere"
    (-> decl:Mathlib:EuclideanGeometry.Sphere), ranked 267/292 by weight and never
    shipped, so the card showed 200 rows of bulk `depends` and the reader could not
    learn the cell has a cross-database bond at all. Same for Identity function's
    `special_case` (771/789) and Fourier series' `co-statement` (249/260) — the
    entire payoff of the TheoremGraph ingest.

    Round-robin over kinds, rarest first, so every kind the cell HAS claims a slot
    before any kind repeats; survivors are re-sorted heaviest-first because that is
    the reading order the card renders in. A synapse carrying several kinds counts
    for all of them, so representing a rare kind is never wasted. Guaranteed
    complete: with <=11 kinds and cap=200 the first pass always seats every kind.
    """
    if len(syns) <= cap:
        return sorted(syns, key=lambda e: (-e["w"], e["id"]))
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for entry in syns:
        for kind in entry["kinds"]:
            by_kind[kind].append(entry)
    for arr in by_kind.values():
        arr.sort(key=lambda e: (-e["w"], e["id"]))   # heaviest first WITHIN a kind
    # rarest kind first: the lone `generalization` must claim its slot before
    # `depends` (~10:1 more common) spends the budget
    order = sorted(by_kind, key=lambda k: (len(by_kind[k]), k))
    picked: dict[str, dict] = {}
    cursor: dict[str, int] = {k: 0 for k in order}
    while len(picked) < cap:
        progressed = False
        for kind in order:
            if len(picked) >= cap:
                break
            arr = by_kind[kind]
            i = cursor[kind]
            while i < len(arr) and arr[i]["id"] in picked:
                i += 1               # already seated by an earlier (rarer) kind
            cursor[kind] = i + 1
            if i < len(arr):
                picked[arr[i]["id"]] = arr[i]
                progressed = True
        if not progressed:
            break
    return sorted(picked.values(), key=lambda e: (-e["w"], e["id"]))


def load_frontier(cells: dict[str, dict]) -> tuple[list[dict], dict]:
    """Read brain/data/frontier.jsonl (build_frontier.py), validated against the
    CURRENT cell set — fail-soft when absent (the tree just keeps the old blob).

    A stale frontier row must never place a vanished cell (S5 would go red) or
    double-place a now-formalized one (the bubble tree would count it twice), so
    both are dropped — COUNTED, never silently ('extreme minority' rule), along
    with the coverage the partition contract promises: every currently-homeless
    cell claimed by exactly one area.
    """
    path = BRAIN_DATA / "frontier.jsonl"
    stats = {"areas": 0, "cells": 0, "unknown_dropped": 0, "formalized_dropped": 0,
             "homeless": 0, "unclaimed": 0}
    if not path.exists():
        print("  ! no brain/data/frontier.jsonl — frontier areas not emitted; "
              "run python3 brain/build_frontier.py first", file=sys.stderr)
        return [], stats
    homeless = {cid for cid, c in cells.items()
                if not any(o.get("kind") == "decl" for o in c["organs"])}
    stats["homeless"] = len(homeless)
    rows, claimed = [], set()
    for _, row in _iter(path):
        keep = []
        for cid in row.get("cells", []):
            if cid not in cells:
                stats["unknown_dropped"] += 1
            elif cid not in homeless:
                stats["formalized_dropped"] += 1
            else:
                keep.append(cid)
        claimed.update(keep)
        if keep:
            kept_set = set(keep)
            out = {**row, "cells": sorted(keep), "n": len(keep)}
            # top chips must never point at cells just dropped as stale
            if out.get("top"):
                n_top = len(out["top"])
                out["top"] = [t for t in out["top"] if t.get("cell") in kept_set]
                if len(out["top"]) != n_top:
                    stats["stale_top"] = stats.get("stale_top", 0) + \
                        (n_top - len(out["top"]))
            # prox arrays are PARALLEL to the row's cells — a stale-drop must
            # re-align them by index or every score lands on the wrong cell
            # (worse than missing data). Malformed arrays (length mismatch)
            # never ship: dropped LOUDLY, counted.
            prox = row.get("prox")
            if prox:
                src_cells = row.get("cells", [])
                if all(isinstance(v, list) and len(v) == len(src_cells)
                       for v in prox.values()):
                    # Re-align to the FINAL emitted order (out["cells"] is
                    # sorted), never to source order: parallel-array
                    # correctness must not silently depend on the frontier
                    # row already being sorted. Identity permutation (the
                    # normal case) ships prox verbatim, preserving the S8
                    # byte-copy pin.
                    pos = {cid: i for i, cid in enumerate(src_cells)}
                    idx = [pos[cid] for cid in out["cells"]]
                    if idx != list(range(len(src_cells))):
                        out["prox"] = {k: [v[i] for i in idx]
                                       for k, v in prox.items()}
                    if len(keep) != len(src_cells):
                        stats["stale_prox"] = stats.get("stale_prox", 0) + \
                            (len(src_cells) - len(keep))
                else:
                    out.pop("prox", None)
                    stats["malformed_prox"] = stats.get("malformed_prox", 0) + 1
                    print(f"  ! MALFORMED prox on {row.get('id')}: array "
                          f"lengths != len(cells) — prox NOT shipped for this "
                          f"area; rerun python3 brain/build_frontier.py",
                          file=sys.stderr)
            rows.append(out)
        stats["areas"] += 1
        stats["cells"] += len(keep)
    stats["unclaimed"] = len(homeless - claimed)
    pct = 100 * (len(homeless) - stats["unclaimed"]) / max(len(homeless), 1)
    print(f"frontier:  {stats['areas']} areas claim {stats['cells']}/"
          f"{len(homeless)} homeless cells ({pct:.1f}% coverage)", file=sys.stderr)
    for key, why in (("unknown_dropped", "cells no longer in cells.jsonl"),
                     ("formalized_dropped", "cells that grew a decl organ"),
                     ("unclaimed", "homeless cells no area claims")):
        if stats[key]:
            print(f"  ! STALE frontier.jsonl: {stats[key]} {why} — rerun "
                  f"python3 brain/build_frontier.py", file=sys.stderr)
    return rows, stats


def main() -> int:
    t0 = time.monotonic()
    cell_meta, cell_rows = load_jsonl(BRAIN_DATA / "cells.jsonl")
    syn_meta, synapses = load_jsonl(BRAIN_DATA / "synapses.jsonl")
    cells = {c["id"]: c for c in cell_rows}
    print(f"{len(cells)} cells / {len(synapses)} synapses", file=sys.stderr)
    frontier_rows, frontier_stats = load_frontier(cells)

    nodes: dict[str, dict] = {}
    for _, node in _iter(BRAIN_DATA / "nodes.jsonl"):
        nodes[node["id"]] = node

    parent: dict[str, str] = {}
    for _, edge in _iter(BRAIN_DATA / "edges.jsonl"):
        if edge["kind"] == "contains" and edge["dst"].startswith("path:"):
            parent[edge["dst"]] = edge["src"]

    # ---- synapses per endpoint (undirected: one list, heaviest first) ---------
    # An endpoint may be a SUPERCELL: a rule-5 field concept ("Linear algebra") owns
    # no cell but keeps its bonds, which hang off the module that holds it.
    by_cell: dict[str, list] = defaultdict(list)
    for syn in synapses:
        entry_a = {"id": syn["dst"], "w": syn["weight"], "kinds": syn["kinds"]}
        entry_b = {"id": syn["src"], "w": syn["weight"], "kinds": syn["kinds"]}
        traces = [trim_trace(t) for t in pick_traces(syn["traces"], SHARD_TRACE_CAP)]
        dropped = len(syn["traces"]) - len(traces) + syn.get("truncated", 0)
        for entry in (entry_a, entry_b):
            entry["traces"] = traces
            if dropped:
                entry["tt"] = len(syn["traces"]) + syn.get("truncated", 0)
        by_cell[syn["src"]].append(entry_a)
        by_cell[syn["dst"]].append(entry_b)

    # ---- containment: supercell -> the CELLS that render inside it ------------
    sup_cells: dict[str, list[str]] = defaultdict(list)
    for cid, cell in cells.items():
        for sup in cell.get("supercells") or []:
            sup_cells[sup].append(cid)
    sup_children: dict[str, list[str]] = defaultdict(list)
    for path, par in parent.items():
        sup_children[par].append(path)

    def breadcrumb(sup: str | None) -> list[dict]:
        chain: list[dict] = []
        cur = sup
        while cur:
            node = nodes.get(cur) or {}
            chain.insert(0, {"id": cur, "label": node.get("label") or cur.split("/")[-1]})
            cur = parent.get(cur)
        return chain

    # ---- cell entries --------------------------------------------------------
    serialized: dict[str, str] = {}
    n_syn_attached = 0
    for cid, cell in sorted(cells.items()):
        syns = by_cell.get(cid, [])
        # every bond KIND before the cap is spent on more of the heaviest (pick_synapses)
        kept = pick_synapses(syns, SYN_CAP)
        n_syn_attached += len(kept)
        sups = cell.get("supercells") or []
        entry = {
            "cell": {k: v for k, v in cell.items() if k != "organs"},
            "organs": [organ_payload(o, nodes) for o in cell["organs"]],
            "syn": kept,
            "counts": {"syn": len(syns), "organs": len(cell["organs"])},
        }
        if len(kept) < len(syns):
            entry["truncated"] = {"syn": len(syns) - len(kept)}
        if sups:
            # a cell may span modules; the card shows the shallowest as its home
            entry["breadcrumb"] = breadcrumb(min(sups, key=lambda s: (s.count("/"), s)))
        serialized[cid] = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    # ---- prefix-shard (build_shards' scheme, verbatim) ------------------------
    def shard_json(ids: list[str]) -> str:
        return "{" + ",".join(f"{json.dumps(i, ensure_ascii=False)}:{serialized[i]}"
                              for i in sorted(ids)) + "}"

    leaves: dict[str, list[str]] = {}
    queue: list[tuple[int, list[str]]] = [(MIN_KEY_LEN, list(cells))]
    while queue:
        length, ids = queue.pop()
        groups: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            groups[shard_key(i, length)].append(i)
        for key, arr in groups.items():
            if (length < MAX_KEY_LEN and len(arr) > 1
                    and len(shard_json(arr).encode()) > MAX_SHARD_BYTES):
                queue.append((length + 1, arr))
            else:
                leaves[key] = sorted(arr)

    gen = cell_meta.get("generated_at", "")

    # ---- aliases.json: EVERY organ id -> its cell id --------------------------
    # The compat layer. Breaking v2 cell ids/API/MCP is authorized, but /brain#Q181296,
    # /api/brain/*, the MCP tools and bench must all keep resolving — they address
    # organs (QIDs, decl names, slugs, page ids), and this is the only map from those
    # to the atom that now owns them. C4 guarantees it is a FUNCTION.
    organ_to_cell: dict[str, str] = {}
    slugs: dict[str, str] = {}
    decls: dict[str, str] = {}
    for cid, cell in cells.items():
        for organ in cell["organs"]:
            organ_to_cell[organ["id"]] = cid
            if organ["kind"] == "decl":
                decls[organ["id"].split(":", 2)[2]] = cid
            elif organ["kind"] == "article":
                slugs[organ["id"]] = cid
            elif organ["kind"] == "concept":
                node = nodes.get(organ["id"]) or {}
                if node.get("slug"):
                    slugs.setdefault(node["slug"], cid)
    supercell_organs = cell_meta.get("supercell_organs", {})
    for path, organs in supercell_organs.items():
        for organ in organs:
            # a supercell organ resolves to its SUPERCELL (rule 5): "Linear algebra"
            # must land on path:Mathlib/LinearAlgebra, not on any cell
            organ_to_cell.setdefault(organ["id"], path)

    aliases = {
        "_meta": {"schema": "brain/SCHEMA.md#v3", "generated_at": gen,
                  "note": "organs -> the cell that owns it (a supercell for rule-5 "
                          "organs); decls/slugs are convenience indexes",
                  "counts": {"organs": len(organ_to_cell), "decls": len(decls),
                             "slugs": len(slugs)}},
        "organs": {k: organ_to_cell[k] for k in sorted(organ_to_cell)},
        "decls": {k: decls[k] for k in sorted(decls)},
        "slugs": {k: slugs[k] for k in sorted(slugs)},
    }

    # ---- labels.json: search over ATOMS ---------------------------------------
    # `aka` carries every organ label, so searching "vector space" finds the Module
    # atom even though the atom is named "Module (mathematics)" — the v2 search
    # returned the separate Vector-space node, which no longer exists.
    #
    # RANKED by what a human would type, not alphabetically. `aka` used to be
    # sorted(...)[:8], and ASCII orders uppercase before lowercase, so long arXiv
    # STATEMENT titles ("Gravity and its wonders: braneworlds and holography") won
    # slots over the names people actually search: measured, `Module.Dual`,
    # `Surjective function` and `extDeriv` were all UNFINDABLE, because the client's
    # search index is built ONLY from this file. A paper title is not a name for the
    # atom, so statement organs yield first and every searchable label is kept
    # (max 12 on live data, under AKA_CAP). Whatever a cap drops is COUNTED below
    # and declared in manifest.caps — never silently (SCHEMA).
    labels = []
    aka_dropped = 0
    for cid, cell in sorted(cells.items()):
        seen_aka: dict[str, int] = {}
        for organ in cell["organs"]:
            label = organ.get("label")
            if not label or label == cell["label"]:
                continue
            rank = 0 if organ["kind"] in AKA_SEARCHABLE else 1
            # an organ label may arrive under several kinds; the best rank wins
            seen_aka[str(label)] = min(seen_aka.get(str(label), rank), rank)
        aka = [a for _, a in sorted((r, a) for a, r in seen_aka.items())]
        aka_dropped += max(0, len(aka) - AKA_CAP)
        row = {"id": cid, "label": cell["label"]}
        if cell.get("f"):
            row["f"] = cell["f"]
        if aka:
            row["aka"] = aka[:AKA_CAP]
        sups = cell.get("supercells") or []
        if sups:
            row["p"] = min(sups, key=lambda s: (s.count("/"), s))
        labels.append(row)
    labels.sort(key=lambda r: (-len(r.get("aka") or []), r["label"]))

    # ---- supercells.json: the containment tree, leaves are CELLS ---------------
    # `fa` = subtree-aggregate facet bits. A supercell carries no tag bits of its
    # own, so without this a facet chip dims EVERY folder — "showing 0 of N" over a
    # grey canvas, the bug reported against v2 on 2026-07-10. Same fix as v2's
    # aggregate_facets, recomputed over cells.
    fa: dict[str, int] = defaultdict(int)
    for cid, cell in cells.items():
        bits = cell.get("f", 0)
        if not bits:
            continue
        for sup in cell.get("supercells") or []:
            cur = sup
            while cur is not None:
                if fa[cur] & bits == bits:
                    break          # ancestors already carry these bits
                fa[cur] |= bits
                cur = parent.get(cur)

    # subtree cell counts, so a root/folder can say how much it actually holds
    subtree_cells: dict[str, int] = defaultdict(int)
    for cid, cell in cells.items():
        seen_paths: set[str] = set()
        for sup in cell.get("supercells") or []:
            cur = sup
            while cur is not None and cur not in seen_paths:
                seen_paths.add(cur)
                cur = parent.get(cur)
        for p in seen_paths:  # a multi-module cell counts ONCE per ancestor
            subtree_cells[p] += 1

    supercells = {}
    for path in sorted(set(sup_cells) | set(sup_children) | set(parent)):
        node = nodes.get(path) or {}
        row = {"label": node.get("label") or path.split("/")[-1]}
        if fa.get(path):
            row["fa"] = fa[path]
        if parent.get(path):
            row["parent"] = parent[path]
        if sup_children.get(path):
            row["children"] = sorted(sup_children[path])
        if sup_cells.get(path):
            row["cells"] = sorted(sup_cells[path])
        if supercell_organs.get(path):
            row["organs"] = supercell_organs[path]
        # A supercell's own synapses (rule-5 field-concept bonds), heaviest first.
        # Traces are DELIBERATELY omitted: this file is fetched eagerly to draw the
        # bubble tree, and 9,529 supercell synapses x ~380B of evidence would treble
        # it (2.0 -> ~5.6 MB) to carry evidence nobody has clicked yet. The drawer
        # fetches them on demand — `traces` below says exactly where from, so the
        # omission is declared in the artifact rather than discovered by a reader.
        if by_cell.get(path):
            syns = by_cell[path]
            kept = pick_synapses(syns, SYN_CAP)   # every KIND first, as for cells
            row["syn"] = [{k: v for k, v in e.items() if k != "traces"}
                          for e in kept]
            row["counts"] = {"syn": len(syns)}
            # SYN_CAP applies here too, so it must be COUNTED here too. Without this
            # a reader (and /api/brain/neighborhood, which reads these rows straight)
            # can only infer the drop by comparing len(syn) against counts.syn — and
            # the API instead reported truncated:false while withholding up to 728 of
            # 928 synapses on path:Mathlib/Algebra. A cap is never silent (SCHEMA).
            if len(kept) < len(syns):
                row["truncated"] = {"syn": len(syns) - len(kept)}
        supercells[path] = row

    # ---- frontier areas: parentless tree rows, exactly like a path: supercell -
    # `frontier:<Area>` lists the homeless cells the FRONTIER CONTRACT assigned to
    # it (brain/build_frontier.py; brain/SCHEMA.md "Frontier layer"). Listing them
    # in `cells` is what drains the client's derived "no formal home" bucket —
    # ensureTree computes unplaced = labels minus the union of every row's cells.
    # Parentless => they join `roots` below and render beside the library roots.
    # `fa` is computed here (the ancestor walk above only sees `supercells` on
    # cell rows, which homeless cells don't have) so facet chips never grey a
    # frontier folder that holds matching cells.
    misaligned_prox = []
    for row in frontier_rows:
        entry = {"label": row.get("label") or row["id"].split(":", 1)[1],
                 "frontier": True, "cells": row["cells"]}
        area_fa = 0
        for cid in row["cells"]:
            area_fa |= cells[cid].get("f", 0)
        if area_fa:
            entry["fa"] = area_fa
        if row.get("near"):
            entry["near"] = row["near"]
        if row.get("mean_stateability") is not None:
            entry["stateability"] = row["mean_stateability"]
        if row.get("top"):
            entry["top"] = row["top"]
        # formal-proximity arrays pass through VERBATIM (PROXIMITY CONTRACT:
        # the shard row ships frontier.jsonl's per-cell scores unchanged —
        # load_frontier already re-aligned them if stale members dropped;
        # test_cell_shards S5 asserts the pass-through). Belt over braces: a
        # misaligned array set must NEVER ship (scores on the wrong cells).
        if row.get("prox"):
            if all(isinstance(v, list) and len(v) == len(row["cells"])
                   for v in row["prox"].values()):
                entry["prox"] = row["prox"]
            else:
                misaligned_prox.append(row["id"])
        supercells[row["id"]] = entry
    if misaligned_prox:
        print(f"  ! MISALIGNED frontier prox on {len(misaligned_prox)} area(s) "
              f"(array lengths != the kept cells after stale-drop) — prox NOT "
              f"shipped for: {misaligned_prox[:3]}; rerun "
              f"python3 brain/build_frontier.py", file=sys.stderr)
    n_sup_syn = sum(len(r.get("syn") or []) for r in supercells.values())
    sup_doc = {"_meta": {"schema": "brain/SCHEMA.md#v3", "generated_at": gen,
                         "traces": "supercell `syn` rows carry NO traces (byte budget: "
                                   "this file is fetched eagerly). The lazy sidecar "
                                   "traces/<key>.json ships them per synapse "
                                   "(lookup: manifest `traces`); "
                                   "/api/brain/neighborhood?id=<path:…> and "
                                   "brain/query.py --full serve the untruncated set.",
                         "counts": {"supercells": len(supercells),
                                    "with_cells": sum(1 for r in supercells.values()
                                                      if r.get("cells")),
                                    "synapse_rows": n_sup_syn,
                                    # the frontier layer, so its presence (and the
                                    # coverage its partition promises) is declared
                                    # in the artifact, never inferred
                                    "frontier_areas": len(frontier_rows),
                                    "frontier_cells": frontier_stats["cells"],
                                    "frontier_homeless": frontier_stats["homeless"],
                                    "frontier_unclaimed": frontier_stats["unclaimed"]}},
               "roots": sorted(p for p in supercells if p not in parent),
               "supercells": supercells}

    # ---- explorer.json: the WHOLE flat graph, positions precomputed ------------
    # v2 shipped seeds + a client force sim and STILL had to cap the draw at 4,000
    # edges — which is what produced the phantom-ring bug (edges pointing at nodes
    # that were never drawn).
    #
    # Edges are index triples [i, j, w] into `nodes`, not {src,dst} id objects: ids
    # average ~11 chars and repeat twice per edge, so objects cost ~4x. That is the
    # difference between shipping the COMPLETE cell graph and silently dropping a
    # chunk of it to fit a byte budget. No cap, so no phantoms are possible.
    order = sorted(cell_rows, key=lambda c: c["id"])
    index = {c["id"]: i for i, c in enumerate(order)}
    explorer = {
        "_meta": {"schema": "brain/SCHEMA.md#v3", "generated_at": gen,
                  "truncated": False,
                  "format": "edges are [node_index, node_index, weight] into `nodes`",
                  "counts": {"nodes": len(order), "edges": len(synapses)}},
        "nodes": [{"id": c["id"], "label": c["label"], "xy": c["xy"],
                   **({"f": c["f"]} if c.get("f") else {}),
                   **({"p": min(c["supercells"], key=lambda s: (s.count("/"), s))}
                      if c.get("supercells") else {})}
                  for c in order],
        # Supercell endpoints are excluded here BY DESIGN, not truncated: the
        # explorer is the flat CELL graph, and a module-level bond belongs to the
        # bubble view (it ships on supercells.json, as v2's rollups did). Counted
        # below so the omission is never silent.
        "edges": sorted(([index[s["src"]], index[s["dst"]], s["weight"]]
                         for s in synapses
                         if s["src"] in index and s["dst"] in index),
                        key=lambda e: (-e[2], e[0], e[1])),
    }
    n_sup_edges = len(synapses) - len(explorer["edges"])
    explorer["_meta"]["counts"]["edges"] = len(explorer["edges"])
    explorer["_meta"]["counts"]["supercell_edges_on_supercells_json"] = n_sup_edges
    explorer_blob = json.dumps(explorer, ensure_ascii=False, separators=(",", ":"))
    rows = explorer["edges"]
    if len(explorer_blob.encode()) > EXPLORER_BUDGET:
        # Never truncate silently. If the complete graph ever outgrows the budget,
        # say so loudly rather than shipping a quietly partial map.
        print(f"  ! explorer.json is {len(explorer_blob.encode()) / 1e6:.1f} MB, over "
              f"the {EXPLORER_BUDGET / 1e6:.1f} MB budget — shipping it COMPLETE "
              f"anyway; compact the format or split the view", file=sys.stderr)

    # ---- trace sidecar: lazy evidence for supercell-involving synapses ---------
    # supercells.json ships its `syn` rows TRACELESS on purpose (fetched eagerly —
    # traces would treble it), and the client can only re-hydrate a cell<->cell
    # pair from the partner cell's shard entry. Every cell<->path and path<->path
    # synapse therefore rendered an EMPTY evidence drawer. The sidecar ships
    # exactly those rows' traces as lazy bucket files (traces/<key>.json) the
    # drawer fetches on first open — the cell shards' longest-prefix scheme applied
    # to the pair key "<src>|<dst>", so the client computes the bucket filename
    # from the pair alone via manifest.traces.files. supercells.json itself does
    # not grow (contract), and the cap counts what it drops: per-row `tt` is the
    # synapse's TRUE bond total, never silent (SCHEMA).
    sidecar: dict[str, str] = {}
    sidecar_trimmed = 0
    sidecar_misordered: list[tuple[str, str]] = []
    for syn in synapses:
        if syn["src"] in cells and syn["dst"] in cells:
            continue        # cell<->cell: traces already ship on both cell entries
        if not syn["src"] < syn["dst"]:
            # the client derives the key as min(a,b)+"|"+max(a,b); a row stored
            # out of order would be UNREACHABLE — loud here, RED in the tests
            sidecar_misordered.append((syn["src"], syn["dst"]))
        pair = f'{syn["src"]}|{syn["dst"]}'
        if pair in sidecar:
            raise SystemExit(f"duplicate synapse pair {pair!r} (C6 broken) — the "
                             "sidecar would silently shadow one row")
        traces = [trim_trace(t)
                  for t in pick_traces(syn["traces"], SIDECAR_TRACE_CAP)]
        tt = len(syn["traces"]) + syn.get("truncated", 0)  # the TRUE bond total
        if tt > len(traces):
            sidecar_trimmed += 1
        sidecar[pair] = json.dumps({"tt": tt, "traces": traces},
                                   ensure_ascii=False, separators=(",", ":"))
    if sidecar_misordered:
        print(f"  ! {len(sidecar_misordered)} sidecar pair keys are NOT src<dst "
              f"(e.g. {sidecar_misordered[:2]}) — the client cannot derive their "
              f"bucket; test_cell_shards.py will go RED", file=sys.stderr)
    if len(sidecar) != n_sup_edges:
        print(f"  ! sidecar rows ({len(sidecar)}) != the explorer's declared "
              f"supercell split ({n_sup_edges}) — the accounting no longer "
              f"reconciles; test_cell_shards.py will go RED", file=sys.stderr)

    def bucket_json(pairs: list[str]) -> str:
        return "{" + ",".join(f"{json.dumps(p, ensure_ascii=False)}:{sidecar[p]}"
                              for p in sorted(pairs)) + "}"

    trace_leaves: dict[str, list[str]] = {}
    tqueue: list[tuple[int, list[str]]] = [(MIN_KEY_LEN, list(sidecar))]
    while tqueue:
        length, pairs = tqueue.pop()
        tgroups: dict[str, list[str]] = defaultdict(list)
        for p in pairs:
            tgroups[shard_key(p, length)].append(p)
        for key, arr in tgroups.items():
            if (length < MAX_KEY_LEN and len(arr) > 1
                    and len(bucket_json(arr).encode()) > MAX_SHARD_BYTES):
                tqueue.append((length + 1, arr))
            else:
                trace_leaves[key] = sorted(arr)

    # sidecar trace `prov` indexes come from synapses.jsonl; the drawer resolves
    # them against the manifest `prov` table (cells.jsonl's). Identical today —
    # if they ever diverge, ship the synapse table so indexes stay resolvable.
    syn_prov = syn_meta.get("prov", [])
    prov_skew = syn_prov != cell_meta.get("prov", [])
    if prov_skew:
        print("  ! synapses.jsonl prov table differs from cells.jsonl — shipping "
              "it as manifest.traces.prov so sidecar indexes stay resolvable",
              file=sys.stderr)
    sidecar_meta = {
        "caps": {"traces_per_synapse": SIDECAR_TRACE_CAP,
                 "synapses_trimmed": sidecar_trimmed,
                 "selection": "round-robin by kind, rarest first (pick_traces); "
                              "depends witnesses kept to their first pair; per-row "
                              "`tt` is the synapse's TRUE bond total, so any trim "
                              "is counted, never silent"},
        "rows": len(sidecar),
        "dir": "traces",
        "key": "<src>|<dst> exactly as stored on the synapse — src < dst "
               "lexicographically on every row, so min(a,b)+'|'+max(a,b) derives it",
        "lookup": "normalize the pair key exactly like a cell id (lowercase; "
                  "[a-z0-9] kept, anything else '_'; pad with '_' to min_len), "
                  "fetch traces/<the longest key in `files` that prefixes it>.json, "
                  "read bucket[pair] -> {tt, traces}; trace `prov` indexes resolve "
                  "against `prov` HERE when present, else the manifest-level `prov`.",
        "scheme": {"kind": "prefix", "min_len": MIN_KEY_LEN,
                   "max_len": max((len(k) for k in trace_leaves),
                                  default=MIN_KEY_LEN),
                   "max_bytes": MAX_SHARD_BYTES, "pad": PAD},
        **({"prov": syn_prov} if prov_skew else {}),
        "files": {k: len(trace_leaves[k]) for k in sorted(trace_leaves)},
    }

    manifest = {
        "_meta": {
            "schema": "brain/SCHEMA.md#v3",
            "generated_at": gen,   # the cell build's stamp, not wall clock
            "counts": {"cells": len(cells), "shards": len(leaves),
                       "synapses": len(synapses), "synapse_attachments": n_syn_attached,
                       "organs": sum(len(c["organs"]) for c in cell_rows),
                       "frontier_areas": len(frontier_rows)},
            "caps": {"synapses_per_cell": SYN_CAP,
                     "traces_per_synapse": SHARD_TRACE_CAP,
                     # Every cap this file applies is named here, with what it
                     # actually dropped — a cap a reader cannot see is a lie about
                     # the artifact (SCHEMA: a COUNT, not a flag).
                     "aka_per_cell": AKA_CAP,
                     "aka_labels_dropped": aka_dropped,
                     "selection": "synapses_per_cell and traces_per_synapse are "
                                  "selected round-robin by kind, rarest first, so a "
                                  "cap never hides a whole bond KIND; `aka` keeps "
                                  "searchable organ labels (concept/decl/article/"
                                  "page) ahead of arXiv statement titles. Per-cell "
                                  "synapse drops are counted in each entry's "
                                  "`truncated`; supercell rows carry it too.",
                     "evidence_trim": "depends witnesses kept to their first pair; "
                                      "full traces in brain/data/synapses.jsonl "
                                      "(brain/query.py)",
                     "trace_sidecar": "supercell-involving synapses (traceless in "
                                      "supercells.json) ship their traces lazily "
                                      "in traces/<key>.json — caps + lookup in "
                                      "the manifest `traces` section"},
            "lookup": "normalize the cell id (lowercase; [a-z0-9] kept, anything else "
                      "'_'; pad with '_' to min_len), fetch <the longest key in "
                      "`shards` that prefixes it>.json, read shard[id]; `prov` fields "
                      "index into `prov` below. Any ORGAN id resolves via aliases.json.",
        },
        "scheme": {"kind": "prefix", "min_len": MIN_KEY_LEN,
                   "max_len": max(len(k) for k in leaves),
                   "max_bytes": MAX_SHARD_BYTES, "pad": PAD},
        # Roots carry the library metadata the v2 manifest had (library_kind,
        # n_decls, n_files) plus the cell count: without them the renderer's
        # math/CS/physics/tooling Libraries filter has nothing to filter ON, and it
        # was dropped as dead UI. `cells` is the subtree count — 6 of 39 roots hold
        # any cell at all, so the top level can lead with those. Frontier areas are
        # parentless too, so they are roots — but they are not libraries: their
        # label/counts come from their own tree row and they carry `frontier: true`
        # so a consumer can keep the two apart.
        "roots": [({"id": p, "frontier": True,
                    "label": supercells[p]["label"],
                    "cells": len(supercells[p].get("cells") or []),
                    **({"fa": supercells[p]["fa"]}
                       if supercells[p].get("fa") else {})}
                   if supercells[p].get("frontier") else
                   {"id": p,
                    "label": (nodes.get(p) or {}).get("label") or p[5:],
                    **{k: (nodes.get(p) or {})[k]
                       for k in ("library_kind", "n_decls", "n_files")
                       if (nodes.get(p) or {}).get(k) is not None},
                    **({"cells": subtree_cells[p]} if subtree_cells.get(p) else {}),
                    **({"fa": fa[p]} if fa.get(p) else {})})
                  for p in sup_doc["roots"]],
        "prov": cell_meta.get("prov", []),
        "shards": {k: len(leaves[k]) for k in sorted(leaves)},
        # the trace sidecar's _meta (TRACE-SIDECAR CONTRACT): the manifest is the
        # one file the client always holds, so the bucket key set lives here and
        # supercells.json does not grow.
        "traces": sidecar_meta,
    }

    # ---- atomic directory swap ------------------------------------------------
    # The scratch dirs live OUTSIDE the published tree. They used to sit in
    # OUT_DIR.parent (site/assets/brain), which build-public.ts cpSyncs wholesale
    # with no filter — so an interrupted build left .cells.tmp behind and the next
    # deploy shipped a duplicate half-written shard set (+1,463 files against
    # Cloudflare's 20,000-file assets ceiling) at a live URL. site/assets is NOT
    # copied wholesale (build-public names individual files there), same filesystem
    # so the renames stay atomic, and build_shards.py's swap of site/assets/brain —
    # which carries cells/ across via its NESTED tuple — never sees them.
    tmp = SCRATCH_DIR / ".cells.tmp"
    old = SCRATCH_DIR / ".cells.old"
    for stale in (tmp, old):
        if stale.exists():
            shutil.rmtree(stale)
    tmp.mkdir(parents=True)

    sizes = {}
    for key, ids in leaves.items():
        payload = shard_json(ids).encode()
        sizes[key] = len(payload)
        (tmp / f"{key}.json").write_bytes(payload)

    # sidecar buckets live in a SUBDIRECTORY: pair keys normalize into the same
    # cell_/path_ prefix space as the cell shards, so a flat layout could collide
    # a bucket filename with a shard filename.
    trace_dir = tmp / "traces"
    trace_dir.mkdir()
    trace_sizes: dict[str, int] = {}
    for key, pairs in trace_leaves.items():
        payload = bucket_json(pairs).encode()
        trace_sizes[key] = len(payload)
        (trace_dir / f"{key}.json").write_bytes(payload)
    t_oversize = [k for k, s in trace_sizes.items() if s > MAX_SHARD_BYTES]
    if t_oversize:
        print(f"  ! {len(t_oversize)} trace bucket(s) over {MAX_SHARD_BYTES} bytes "
              f"(unsplittable key collisions): {t_oversize[:5]} — "
              f"test_cell_shards.py will go RED", file=sys.stderr)

    def dump(name: str, doc) -> int:
        blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        (tmp / name).write_text(blob)
        return len(blob.encode())

    dump("manifest.json", manifest)
    n_labels = dump("labels.json", labels)
    n_alias = dump("aliases.json", aliases)
    n_sup = dump("supercells.json", sup_doc)
    (tmp / "explorer.json").write_text(explorer_blob)

    # frontier_graph.json ships VERBATIM (byte-copy, never re-serialized): the
    # Libraries toggle's client-side re-scoring input must be exactly the file
    # the frontier tests proved the parity law against (test_cell_shards S8
    # pins the bytes).
    # Fail-soft when absent, like frontier.jsonl — but LOUD, and stale-checked
    # against the CURRENT homeless set so a drifted graph never ships silently.
    graph_src = BRAIN_DATA / "frontier_graph.json"
    n_graph = 0
    if graph_src.exists():
        graph_blob = graph_src.read_bytes()
        (tmp / "frontier_graph.json").write_bytes(graph_blob)
        n_graph = len(graph_blob)
        homeless_now = {cid for cid, c in cells.items()
                        if not any(o.get("kind") == "decl" for o in c["organs"])}
        try:
            graph_cells = set(json.loads(graph_blob).get("cells") or [])
        except json.JSONDecodeError:
            graph_cells = None
        if graph_cells != homeless_now:
            print(f"  ! STALE frontier_graph.json: its cells "
                  f"({'unparseable' if graph_cells is None else len(graph_cells)}) "
                  f"!= the current homeless set ({len(homeless_now)}) — the client "
                  f"re-score will diverge from the shipped prox; rerun "
                  f"python3 brain/build_frontier.py", file=sys.stderr)
    else:
        print("  ! no brain/data/frontier_graph.json — the Libraries "
              "toggle has no client re-scoring input; run "
              "python3 brain/build_frontier.py first", file=sys.stderr)

    if OUT_DIR.exists():
        OUT_DIR.rename(old)
    tmp.rename(OUT_DIR)
    if old.exists():
        shutil.rmtree(old)

    total = sum(sizes.values())
    print(f"shards:    {len(cells)} cells -> {len(leaves)} shards "
          f"({total / 1e6:.1f} MB), largest {max(sizes.values()) / 1000:.0f} KB",
          file=sys.stderr)
    print(f"aliases:   {len(organ_to_cell)} organs -> cells ({n_alias / 1e6:.1f} MB)",
          file=sys.stderr)
    print(f"labels:    {len(labels)} atoms ({n_labels / 1e6:.1f} MB)", file=sys.stderr)
    print(f"supercells:{len(supercells)} ({n_sup / 1e6:.1f} MB), "
          f"{sup_doc['_meta']['counts']['with_cells']} hold cells, "
          f"{len(frontier_rows)} frontier areas "
          f"({frontier_stats['cells']} homeless cells claimed, "
          f"{frontier_stats['unclaimed']} unclaimed)", file=sys.stderr)
    print(f"explorer:  {len(cells)} nodes + {len(rows)} edges, complete "
          f"({len(explorer_blob.encode()) / 1e6:.1f} MB)", file=sys.stderr)
    print(f"frontier_graph: "
          + (f"shipped verbatim ({n_graph / 1000:.0f} KB)" if n_graph
             else "NOT SHIPPED (source missing)"), file=sys.stderr)
    print(f"traces:    {len(sidecar)} supercell-synapse rows -> "
          f"{len(trace_leaves)} bucket files "
          f"({sum(trace_sizes.values()) / 1e6:.1f} MB), largest "
          f"{max(trace_sizes.values(), default=0) / 1000:.0f} KB; "
          f"{sidecar_trimmed} rows trimmed to cap {SIDECAR_TRACE_CAP} "
          f"(per-row tt keeps true totals)", file=sys.stderr)
    print(f"-> {OUT_DIR} in {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
