#!/usr/bin/env python3
"""Acceptance for the FRONTIER layer — F1..F8 (brain/SCHEMA.md "Frontier layer").

The frontier partitions the HOMELESS cells (no decl organ) into named areas so
the bubble view's grey "no formal home" blob drains into legible territories.
The contract is a PARTITION plus a deterministic assignment rule, so that is
what gets tested — against the shipped bytes, with the vote re-derived from the
SPEC (not by importing the builder), the same doctrine as test_cell_shards'
shard_key.

  F1  frontier.jsonl meta: generated_at pinned to the cell build, contract counts
  F2  PARTITION: every homeless cell in exactly one area — no drops, no dupes,
      no formalized cell claimed; counts reconcile
  F3  row shape: area ids match the contract regex; `near` names a real
      container; `top` stays within the area
  F4  the assignment rule, re-derived from the SPEC: 3 pinned cells (one per
      tier, from the 2026-08-01 recon simulation) + an independent re-vote of a
      sample of phase-1 cells
  F5  Unsorted stays a minority: share <= the recon prediction (19.2%) + 2pp
  F6  determinism: two builder runs are byte-identical
  F7  mean_stateability recomputes from halo.json (null when no member joins)
  F8  the SHARDS carry it: supercells.json frontier rows match frontier.jsonl,
      fa aggregates member facets, areas are roots, no double placement, and
      the derived "no formal home" bucket really drains (the whole point)
  F9  HALO SHELLS: every area's `shells` partitions its cells exactly; the
      per-cell hop distances match a BFS re-run here from the SPEC (multi-
      source from all decl-organ cells, all synapse kinds, path: endpoints
      excluded — never read from the builder); global sums match
      _meta.halo.shell_counts AND the 2026-08-01 ground truth
      855/454/13/290 (d=1/d=2/d=3/disconnected over 1,612 homeless)
  F10 FRONTIER GRAPH (brain/data/frontier_graph.json — the halo view's
      client-side BFS input): cells == the partition universe exactly;
      `formal` re-derives from the SPEC (per-library summed synapse weight
      over decl-organ neighbors, decl-id fallback for supercell-less ones)
      and its lib names are real library roots; `edges` == every
      frontier<->frontier synapse as in-range deduped sorted [i, j] pairs;
      and THE PARITY LAW: a client BFS written HERE from the spec (all
      libraries enabled) over the emitted graph reproduces the shipped
      shells EXACTLY, cell for cell — the build-side proof that the halo
      view's lazy re-shelling can never drift from the tested partition
      (F6 also pins the file byte-identical across rebuilds)

Run: python3 brain/test_frontier.py
     (after brain/build_frontier.py + brain/build_cell_shards.py)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRONTIER = HERE / "data" / "frontier.jsonl"
GRAPH = HERE / "data" / "frontier_graph.json"
CELLS = HERE / "data" / "cells.jsonl"
SYNAPSES = HERE / "data" / "synapses.jsonl"
EDGES = HERE / "data" / "edges.jsonl"
HALO = ROOT / "manage" / "data" / "halo.json"
SHARD_DIR = ROOT / "site" / "assets" / "brain" / "cells"

AREA_RE = re.compile(r"^frontier:[A-Za-z][A-Za-z0-9_]{0,63}$")
KIND_MULT = {"depends": 3, "invocation": 3}   # the contract's 3x kinds

# Pinned from the 2026-08-01 recon simulation (RECON FINDINGS + census script) —
# one cell per assignment tier. If the data drifts and a pin stops being
# homeless, the check SKIPS with a note rather than failing on stale ground.
PINS = [
    ("cell:Q903783", "frontier:Analysis", "phase-1 vote (Naive set theory)"),
    ("cell:Q979829", "frontier:DeepFrontier_IntegralTransforms",
     "phase-2 MSC 44 (Radon transform)"),
    ("cell:Q1006428", "frontier:Geometry",
     "phase-3 relates hop (Two-body problem in general relativity)"),
]
UNSORTED_CEILING = 0.192 + 0.02   # recon-predicted share (309/1612) + 2pp

# HALO ground truth, measured on 2026-08-01 data (HALO CONTRACT): hop distance
# from the formalized interior over all-kinds cell<->cell synapses. If the data
# drifts these MUST be re-measured (the F9 failure detail says how) — the
# contract pins the exact counts against the current build.
HALO_PIN = {"1": 855, "2": 454, "3": 13, "disc": 290}

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        print(f"  \033[31mFAIL\033[0m {name}: {detail}")
        FAILURES.append(f"{name}: {detail}")


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    meta, rows = {}, []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "_meta" in row and len(row) == 1:
                meta = row["_meta"]
                continue
            rows.append(row)
    return meta, rows


def main() -> int:
    for path in (FRONTIER, CELLS, SYNAPSES):
        if not path.exists():
            print(f"missing {path} — run the builders first")
            return 1
    meta, rows = load_jsonl(FRONTIER)
    cells_meta, cell_rows = load_jsonl(CELLS)
    cells = {c["id"]: c for c in cell_rows}
    homeless = {cid for cid, c in cells.items()
                if not any(o.get("kind") == "decl" for o in c["organs"])}
    print(f"{len(rows)} areas over {len(homeless)} homeless cells "
          f"(of {len(cells)})\n")

    # ---- F1: meta -----------------------------------------------------------
    check("F1 _meta carries the contract counts",
          set(meta.get("counts", {})) >= {"homeless", "assigned", "unsorted"},
          f"counts keys: {sorted(meta.get('counts', {}))}")
    check("F1 generated_at is the CELL build's stamp (determinism input-pin)",
          meta.get("generated_at") == cells_meta.get("generated_at"),
          f"{meta.get('generated_at')!r} != {cells_meta.get('generated_at')!r}")
    check("F1 a method string names the assignment pipeline",
          "vote" in (meta.get("method") or ""), f"method={meta.get('method')!r}")

    # ---- F2: THE PARTITION --------------------------------------------------
    # 'Extreme minority' rule: the coverage percentage is asserted, not implied.
    seen: Counter = Counter()
    for r in rows:
        seen.update(r["cells"])
    dupes = [c for c, n in seen.items() if n > 1]
    check("F2 no cell sits in two areas", not dupes,
          f"{len(dupes)} duplicated, e.g. {dupes[:3]}")
    dropped = sorted(homeless - set(seen))
    check("F2 every homeless cell is claimed (100% coverage — no drops)",
          not dropped, f"{len(dropped)} unclaimed, e.g. {dropped[:3]}")
    invented = sorted(set(seen) - homeless)
    check("F2 no formalized (or unknown) cell is claimed", not invented,
          f"{len(invented)} non-homeless claimed, e.g. {invented[:3]}")
    check("F2 row n == len(cells) on every row",
          all(r["n"] == len(r["cells"]) for r in rows))
    unsorted_row = next((r for r in rows if r["id"] == "frontier:Unsorted"), None)
    n_unsorted = len(unsorted_row["cells"]) if unsorted_row else 0
    counts = meta.get("counts", {})
    check("F2 counts reconcile: sum(n) == homeless == counts.homeless; "
          "assigned + unsorted == homeless",
          sum(r["n"] for r in rows) == len(homeless) == counts.get("homeless")
          and counts.get("assigned", -1) + counts.get("unsorted", -1)
          == len(homeless)
          and counts.get("unsorted") == n_unsorted,
          f"sum={sum(r['n'] for r in rows)} homeless={len(homeless)} "
          f"counts={counts} unsorted_row={n_unsorted}")

    # ---- F3: row shape ------------------------------------------------------
    bad_ids = [r["id"] for r in rows if not AREA_RE.match(r["id"])]
    check("F3 every area id matches ^frontier:[A-Za-z][A-Za-z0-9_]{0,63}$",
          not bad_ids, f"{bad_ids[:3]}")
    # `near` must name a real container: some cell's supercell path (or an
    # ancestor prefix of one) — the formal home the area claims to sit beside
    real_paths: set[str] = set()
    for c in cell_rows:
        for sup in c.get("supercells") or []:
            parts = sup.split(":", 1)[1].split("/")
            for i in range(1, len(parts) + 1):
                real_paths.add("path:" + "/".join(parts[:i]))
    bad_near = [(r["id"], r["near"]) for r in rows
                if r["near"] is not None and r["near"] not in real_paths]
    check("F3 `near` is null or names a real container path", not bad_near,
          f"{bad_near[:3]}")
    bad_top = [r["id"] for r in rows
               if len(r.get("top", [])) > 12
               or any(t["cell"] not in set(r["cells"]) for t in r.get("top", []))]
    check("F3 `top` has <=12 rows and stays within the area", not bad_top,
          f"{bad_top[:3]}")
    check("F3 every row has a non-empty label",
          all((r.get("label") or "").strip() for r in rows))

    # ---- F4: the assignment rule, re-derived from the SPEC ------------------
    assigned = {c: r["id"] for r in rows for c in r["cells"]}
    decl_cells = set(cells) - homeless

    def areas_of(cid: str) -> list[str]:
        out = set()
        for sup in cells[cid].get("supercells", []):
            parts = sup.split(":", 1)[1].split("/")
            lib, top = parts[0], (parts[1] if len(parts) > 1 else None)
            out.add((top if top else "Mathlib") if lib == "Mathlib"
                    else (f"{lib}_{top}" if top else lib))
        return sorted(out)

    nbrs: dict[str, list] = defaultdict(list)
    syn_rows = load_jsonl(SYNAPSES)[1]   # kept raw: F10 re-derives from them
    for row in syn_rows:
        kinds = row.get("kinds", {})
        eff = sum(n * KIND_MULT.get(k, 1) for k, n in kinds.items())
        nbrs[row["src"]].append((row["dst"], kinds, eff))
        nbrs[row["dst"]].append((row["src"], kinds, eff))

    def revote(cid: str) -> str | None:
        """Phase 1 per the CONTRACT text: weighted vote of formalized neighbors,
        depends/invocation x3, multi-area neighbors split equally, winner by
        (-score, name), lexicographic tie-break."""
        votes: Counter = Counter()
        n_formal = 0
        for other, _kinds, eff in nbrs.get(cid, []):
            if other.startswith("path:") or other not in decl_cells:
                continue
            n_formal += 1
            ars = areas_of(other)
            for a in ars:
                votes[a] += eff / len(ars)
        if not n_formal or not votes:
            return None
        return sorted(votes.items(),
                      key=lambda kv: (-round(kv[1], 9), kv[0]))[0][0]

    # (a) three pinned cells, one per tier
    for cid, want, why in PINS:
        if cid not in homeless:
            print(f"  SKIP pin {cid} ({why}) — no longer homeless (data drift)")
            continue
        check(f"F4 pinned {why}: {cid} -> {want}",
              assigned.get(cid) == want, f"got {assigned.get(cid)!r}")
    # (b) an independent re-vote of sampled phase-1 cells
    votable = sorted(c for c in homeless if revote(c) is not None)
    sample = votable[::40] or votable
    diverged = [(c, assigned.get(c), "frontier:" + (revote(c) or ""))
                for c in sample if assigned.get(c) != "frontier:" + revote(c)]
    check(f"F4 the spec re-vote agrees on {len(sample)} sampled phase-1 cells",
          not diverged, f"{len(diverged)} diverge, e.g. {diverged[:3]}")

    # ---- F5: Unsorted stays a minority --------------------------------------
    share = n_unsorted / max(len(homeless), 1)
    check(f"F5 Unsorted share {share:.1%} <= ceiling {UNSORTED_CEILING:.1%}",
          share <= UNSORTED_CEILING,
          f"{n_unsorted}/{len(homeless)} — the fallback tiers stopped reaching")

    # ---- F6: determinism ----------------------------------------------------
    before = FRONTIER.read_bytes()
    graph_before = GRAPH.read_bytes() if GRAPH.exists() else None
    proc = subprocess.run([sys.executable, str(HERE / "build_frontier.py")],
                          capture_output=True, text=True)
    check("F6 a rebuild exits 0", proc.returncode == 0,
          (proc.stderr or proc.stdout)[-300:])
    check("F6 two runs are byte-identical (deterministic, seedless)",
          FRONTIER.read_bytes() == before)
    check("F6 frontier_graph.json is byte-identical across rebuilds too",
          GRAPH.exists() and GRAPH.read_bytes() == graph_before,
          "missing before the rebuild" if graph_before is None
          else "the rebuild changed its bytes")

    # ---- F7: mean_stateability recomputes from halo.json --------------------
    halo_frac: dict[str, float] = {}
    if HALO.exists():
        for item in json.loads(HALO.read_text()).get("items", []):
            if item.get("all_frac") is not None:
                halo_frac[item["cell"]] = item["all_frac"]
    bad_state = []
    for r in rows:
        fracs = [halo_frac[c] for c in r["cells"] if c in halo_frac]
        want = round(sum(fracs) / len(fracs), 4) if fracs else None
        if r["mean_stateability"] != want:
            bad_state.append((r["id"], r["mean_stateability"], want))
    check("F7 mean_stateability == halo.json all_frac mean (null when no join)"
          + ("" if HALO.exists() else " [halo.json absent: all null]"),
          not bad_state, f"{bad_state[:3]}")

    # ---- F8: the shards carry it (the artifact the client reads) ------------
    sup_path = SHARD_DIR / "supercells.json"
    if not sup_path.exists():
        check("F8 supercells.json exists", False,
              f"missing {sup_path} — run brain/build_cell_shards.py")
    else:
        sup_doc = json.loads(sup_path.read_text())
        tree = sup_doc["supercells"]
        labels = json.loads((SHARD_DIR / "labels.json").read_text())
        label_f = {r["id"]: r.get("f", 0) for r in labels}
        missing = [r["id"] for r in rows if r["id"] not in tree]
        check("F8 every frontier area is a supercells.json row", not missing,
              f"{missing[:3]}")
        mismatched = [r["id"] for r in rows if r["id"] in tree
                      and tree[r["id"]].get("cells") != r["cells"]]
        check("F8 shard rows list exactly the area's cells", not mismatched,
              f"{mismatched[:3]}")
        not_root = [r["id"] for r in rows if r["id"] not in set(sup_doc["roots"])]
        check("F8 every frontier area is a ROOT (parentless, browsable)",
              not not_root, f"{not_root[:3]}")
        bad_fa = [r["id"] for r in rows if r["id"] in tree]
        bad_fa = [aid for aid in bad_fa
                  if tree[aid].get("fa", 0)
                  != eval_or(label_f, tree[aid].get("cells") or [])]
        check("F8 fa == OR of member cells' facet bits", not bad_fa,
              f"{bad_fa[:3]}")
        # no double placement: a frontier cell must not ALSO sit in a path: row
        path_placed = {c for p, e in tree.items() if p.startswith("path:")
                       for c in e.get("cells") or []}
        doubled = sorted(set(seen) & path_placed)
        check("F8 no frontier cell is also placed under a path: supercell",
              not doubled, f"{len(doubled)}, e.g. {doubled[:3]}")
        # THE BLOB DRAINS — the client derives unplaced = labels minus the union
        # of every row's cells; after the frontier that remainder must contain
        # NO homeless cell (what stays are the few decl-without-container cells)
        placed_all = {c for e in tree.values() for c in e.get("cells") or []}
        residue = {r["id"] for r in labels} - placed_all
        stuck = sorted(residue & homeless)
        check("F8 the derived 'no formal home' bucket holds ZERO homeless cells",
              not stuck, f"{len(stuck)} still in the blob, e.g. {stuck[:3]}")
        print(f"       (bucket residue after the frontier: {len(residue)} "
              f"cells — decl organs with no `contains` parent)")
        # the manifest's root directory carries the areas too, flagged
        manifest = json.loads((SHARD_DIR / "manifest.json").read_text())
        mroots = {e["id"]: e for e in manifest.get("roots", [])}
        bad_m = [r["id"] for r in rows
                 if r["id"] not in mroots or not mroots[r["id"]].get("frontier")]
        check("F8 manifest.roots lists every area with frontier:true", not bad_m,
              f"{bad_m[:3]}")

    # ---- F9: HALO SHELLS — hop distance to the formalized interior ----------
    # Re-derived from the SPEC, never from the builder: multi-source BFS from
    # EVERY decl-organ cell over cell<->cell synapses — ALL kinds conduct,
    # path: (supercell) endpoints never do. d = hops to the nearest formalized
    # cell; unreachable = "disc". (nbrs above already carries every synapse
    # row; the `other in cells` filter is what excludes path: endpoints.)
    dist: dict[str, int] = {cid: 0 for cid in decl_cells}
    bq = deque(sorted(decl_cells))
    while bq:
        cur = bq.popleft()
        for other, _k, _e in nbrs.get(cur, []):
            if other in cells and other not in dist:
                dist[other] = dist[cur] + 1
                bq.append(other)
    want_shell = {c: ("disc" if c not in dist else str(dist[c]))
                  for c in homeless}
    bfs_counts = dict(Counter(want_shell.values()))

    no_shells = [r["id"] for r in rows if not isinstance(r.get("shells"), dict)]
    check("F9 every area row carries a `shells` object", not no_shells,
          f"{no_shells[:3]}")
    shelled = [r for r in rows if isinstance(r.get("shells"), dict)]
    bad_key = [(r["id"], k) for r in shelled for k in r["shells"]
               if k != "disc" and not k.isdigit()]
    check("F9 shell keys are str(d) or 'disc'", not bad_key, f"{bad_key[:3]}")
    empty = [(r["id"], k) for r in shelled
             for k, v in r["shells"].items() if not v]
    check("F9 empty shell keys are omitted", not empty, f"{empty[:3]}")
    # the partition, per area: shells' disjoint union == the row's cells
    bad_part = []
    for r in shelled:
        ids = [c for arr in r["shells"].values() for c in arr]
        if len(ids) != r["n"] or set(ids) != set(r["cells"]):
            bad_part.append(r["id"])
    check("F9 shells PARTITION each area's cells exactly (no drops, no dupes)",
          not bad_part, f"{bad_part[:3]}")
    unsorted_ids = [(r["id"], k) for r in shelled
                    for k, v in r["shells"].items() if v != sorted(v)]
    check("F9 shell member lists are sorted (deterministic bytes)",
          not unsorted_ids, f"{unsorted_ids[:3]}")
    # per-cell agreement with the spec BFS — every cell, not a sample
    diverged = [(r["id"], k, c) for r in shelled
                for k, v in r["shells"].items() for c in v
                if want_shell.get(c) != k]
    check(f"F9 every cell's shell matches the spec BFS distance "
          f"({sum(len(v) for r in shelled for v in r['shells'].values())} "
          f"cells checked)", not diverged,
          f"{len(diverged)} diverge, e.g. {diverged[:3]}")
    # global set math: rows aggregate == _meta.halo == the spec BFS == the pin
    agg = Counter()
    for r in shelled:
        for k, v in r["shells"].items():
            agg[k] += len(v)
    halo_meta = meta.get("halo") or {}
    check("F9 _meta.halo.shell_counts == the sum over area rows",
          dict(agg) == (halo_meta.get("shell_counts") or {}),
          f"rows say {dict(sorted(agg.items()))}, _meta says "
          f"{halo_meta.get('shell_counts')}")
    check("F9 _meta.halo names its method",
          "BFS" in (halo_meta.get("method") or ""),
          f"method={halo_meta.get('method')!r}")
    check("F9 the spec BFS reproduces the shipped counts",
          dict(agg) == bfs_counts,
          f"shipped {dict(sorted(agg.items()))}, BFS re-run says "
          f"{dict(sorted(bfs_counts.items()))}")
    check(f"F9 ground truth holds: shells == {HALO_PIN} "
          f"(d=1/d=2/d=3/disconnected)",
          bfs_counts == HALO_PIN,
          f"BFS says {dict(sorted(bfs_counts.items()))} — if brain/data "
          f"legitimately drifted, re-measure and update HALO_PIN (and the "
          f"HALO CONTRACT counts in brain/SCHEMA.md)")

    # ---- F10: FRONTIER GRAPH — the halo view's client-side BFS input --------
    # Everything below re-derives the FRONTIER GRAPH contract from the SPEC
    # (build_frontier.py docstring / SCHEMA.md), never from the builder's code.
    if not GRAPH.exists():
        check("F10 frontier_graph.json exists", False,
              f"missing {GRAPH} — rerun python3 brain/build_frontier.py")
    else:
        g = json.loads(GRAPH.read_text())
        gmeta = g.get("_meta") or {}
        gcells: list = g.get("cells") or []
        gformal: dict = g.get("formal") or {}
        gedges: list = g.get("edges") or []
        check("F10 generated_at is the CELL build's stamp (determinism pin)",
              gmeta.get("generated_at") == cells_meta.get("generated_at"),
              f"{gmeta.get('generated_at')!r} != "
              f"{cells_meta.get('generated_at')!r}")
        gcounts = gmeta.get("counts") or {}
        check("F10 _meta.counts match the shipped arrays",
              gcounts.get("cells") == len(gcells)
              and gcounts.get("formal") == len(gformal)
              and gcounts.get("edges") == len(gedges),
              f"_meta says {gcounts}, shipped "
              f"{len(gcells)}/{len(gformal)}/{len(gedges)}")
        # the graph's universe IS the partition's universe — same ids, sorted
        check("F10 `cells` == the frontier partition's cells exactly (sorted)",
              gcells == sorted(set(seen)) and gcells == sorted(homeless),
              f"{len(gcells)} graph cells vs {len(set(seen))} partition / "
              f"{len(homeless)} homeless; e.g. graph-only "
              f"{sorted(set(gcells) - homeless)[:3]}, missing "
              f"{sorted(homeless - set(gcells))[:3]}")

        # formal: lib names are real library roots; weights are positive ints
        lib_universe = {sup.split(":", 1)[1].split("/")[0]
                        for c in cell_rows for sup in c.get("supercells") or []}
        fallback_libs = {o["id"].split(":", 2)[1]
                         for c in cell_rows
                         if c["id"] in decl_cells
                         and not (c.get("supercells") or [])
                         for o in c["organs"] if o.get("kind") == "decl"}
        valid_libs = lib_universe | fallback_libs
        bad_lib = [(cid, lib) for cid, lrow in gformal.items()
                   for lib in lrow if lib not in valid_libs]
        check(f"F10 every formal lib is a real library root "
              f"(universe: {sorted(valid_libs)})", not bad_lib,
              f"{len(bad_lib)} unknown, e.g. {bad_lib[:3]}")
        bad_w = [(cid, lib, w) for cid, lrow in gformal.items()
                 for lib, w in lrow.items()
                 if not isinstance(w, int) or w <= 0]
        check("F10 every formal weight is a positive int", not bad_w,
              f"{bad_w[:3]}")

        # formal + edges, re-derived from the SPEC over the raw synapse rows
        def libs_of_formal(cid: str) -> set:
            libs = {sup.split(":", 1)[1].split("/")[0]
                    for sup in cells[cid].get("supercells") or []}
            if not libs:   # supercell-less decl cell: the decl id's <Lib> segment
                libs = {o["id"].split(":", 2)[1] for o in cells[cid]["organs"]
                        if o.get("kind") == "decl"}
            return libs

        gindex = {c: i for i, c in enumerate(gcells)}
        want_formal: dict[str, Counter] = defaultdict(Counter)
        want_edges: set[tuple[int, int]] = set()
        for row in syn_rows:
            s, d = row["src"], row["dst"]
            if s in homeless and d in homeless:
                i, j = gindex.get(s), gindex.get(d)
                if i is not None and j is not None and i != j:
                    want_edges.add((i, j) if i < j else (j, i))
                continue
            for a, b in ((s, d), (d, s)):
                if a in homeless and b in decl_cells:
                    for lib in libs_of_formal(b):
                        want_formal[a][lib] += row["weight"]
        div_formal = [cid for cid in set(want_formal) | set(gformal)
                      if dict(want_formal.get(cid) or {}) != gformal.get(cid)]
        div_detail = [(c, gformal.get(c), dict(want_formal.get(c) or {}))
                      for c in div_formal[:2]]
        check(f"F10 `formal` re-derives from the spec on all "
              f"{len(gformal)} cells (per-lib summed synapse weight)",
              not div_formal,
              f"{len(div_formal)} diverge (cell, shipped, spec), "
              f"e.g. {div_detail}")

        n_cells = len(gcells)
        bad_e = [e for e in gedges
                 if len(e) != 2 or not (0 <= e[0] < n_cells
                                        and 0 <= e[1] < n_cells)
                 or e[0] >= e[1]]
        check("F10 edges are in-range [i, j] index pairs with i < j",
              not bad_e, f"{len(bad_e)} bad, e.g. {bad_e[:3]}")
        etup = [tuple(e) for e in gedges]
        check("F10 edges are sorted + deduped (deterministic bytes)",
              etup == sorted(set(etup)),
              f"{len(etup) - len(set(etup))} dupes / out of order")
        check(f"F10 edges == every frontier<->frontier synapse "
              f"({len(want_edges)} pairs from synapses.jsonl)",
              set(etup) == want_edges,
              f"{len(set(etup) - want_edges)} extra, "
              f"{len(want_edges - set(etup))} missing")

        # THE PARITY LAW, proved on the emitted bytes: the client BFS (spec:
        # all libraries enabled -> d1 = cells with ANY formal lib, BFS outward
        # over edges, unreached = disc) reproduces the SHIPPED shells exactly.
        adj: dict[int, list[int]] = defaultdict(list)
        for i, j in etup:
            adj[i].append(j)
            adj[j].append(i)
        cdist: dict[int, int] = {gindex[c]: 1 for c in gformal if c in gindex}
        cq = deque(sorted(cdist))
        while cq:
            cur = cq.popleft()
            for nxt in adj.get(cur, []):
                if nxt not in cdist:
                    cdist[nxt] = cdist[cur] + 1
                    cq.append(nxt)
        client_shell = {c: ("disc" if gindex[c] not in cdist
                            else str(cdist[gindex[c]])) for c in gcells}
        shipped_shell = {c: k for r in shelled
                         for k, v in r["shells"].items() for c in v}
        parity_div = [(c, client_shell.get(c), shipped_shell.get(c))
                      for c in gcells
                      if client_shell.get(c) != shipped_shell.get(c)]
        check(f"F10 PARITY LAW: the all-libraries client BFS over the graph "
              f"== the shipped shells on ALL {len(gcells)} cells",
              not parity_div,
              f"{len(parity_div)} diverge (cell, client, shipped), "
              f"e.g. {parity_div[:3]}")
        client_counts = dict(Counter(client_shell.values()))
        check(f"F10 client-BFS shell counts == the pinned ground truth "
              f"{HALO_PIN}", client_counts == HALO_PIN,
              f"client BFS says {dict(sorted(client_counts.items()))}")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\n\033[31mRED\033[0m — the frontier contract is broken:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\033[32mGREEN\033[0m — frontier contract holds")
    return 0


def eval_or(label_f: dict[str, int], cell_ids: list[str]) -> int:
    out = 0
    for cid in cell_ids:
        out |= label_f.get(cid, 0)
    return out


if __name__ == "__main__":
    sys.exit(main())
