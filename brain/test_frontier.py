#!/usr/bin/env python3
"""Acceptance for the FRONTIER layer — F1..F11 (brain/SCHEMA.md "Frontier layer").

The frontier partitions the HOMELESS cells (no decl organ) into named areas so
the bubble view's grey "no formal home" blob drains into legible territories,
and scores every one of them with a bond-weighted FORMAL PROXIMITY (the
2026-08-04 replacement for the destroyed halo hop shells — hop counts made
"1 jump over 200 bonds" and "1 jump over one thread" the same tier). The
contract is a PARTITION plus deterministic assignment and scoring rules, so
that is what gets tested — against the shipped bytes, with everything
re-derived from the SPEC (never by importing the builder), the same doctrine
as test_cell_shards' shard_key.

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
  F7  mean_stateability recomputes from the current cells + synapses: for each
      connected ring-1 cell (page organ, no decl organ), all_frac is the share
      of unique cell-neighbors with a decl organ; null when no member joins
  F8  the SHARDS carry it: supercells.json frontier rows match frontier.jsonl,
      fa aggregates member facets, areas are roots, no double placement, and
      the derived "no formal home" bucket really drains (the whole point)
  F9  FORMAL PROXIMITY (PROXIMITY CONTRACT): every area row's `prox` carries
      six arrays (db/dw/ib/iw/s/r) parallel to its cells; every value
      re-derives from the SPEC over the raw synapse rows (direct = summed RAW
      weight into decl-organ cells; bridge = sum of min(bond, direct(u)) over
      frontier neighbors; s = direct + bridge/4 EXACTLY; r = midrank
      percentile of s, ties share); no NaN, nothing negative; the
      _meta.proximity counts reconcile AND match the current pinned ground truth
      852/451/311 (direct/bridged/zero over 1,614 homeless); MONOTONICITY on
      the real data (more direct weight at >= equal bridge weight => strictly
      higher score, full pairwise); and the JACK REGRESSION: a cell with
      hundreds of direct bonds scores >= 100x a weakest-direct cell, which
      outscores a cell whose only path to the core is one bond through one
      near-isolated intermediary (the exact failure of hop tiering)
  F10 FRONTIER GRAPH (brain/data/frontier_graph.json — the client-side
      re-scoring input): cells == the partition universe exactly; `formal`
      re-derives from the SPEC (exact "|"-joined sorted root-SET keys over
      RAW weights, decl-id fallback for supercell-less neighbors) and every
      key names real library roots; `edges` == every frontier<->frontier
      synapse as in-range deduped sorted [i, j, w] triples with the RAW
      weight; and THE PARITY LAW: a client re-score written HERE from the
      spec over the emitted graph reproduces the shipped `s` EXACTLY (exact
      float equality — lambda = 1/4 is lossless in binary floats), both with
      ALL libraries enabled and under proper library SUBSETS (checked against
      an independent raw-synapse restriction) — the build-side proof that the
      Libraries toggle can never drift from the tested scores (F6 also pins
      the file byte-identical across rebuilds)
  F11 SUITABILITY: every cell has an aligned candidate/reason pair; counts
      reconcile; the reported bad top ten stay present but are deprioritized;
      proximity and hub degree never remove a cell from the structural Frontier

Run: python3 brain/test_frontier.py
     (after brain/build_frontier.py + brain/build_cell_shards.py)
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRONTIER = HERE / "data" / "frontier.jsonl"
GRAPH = HERE / "data" / "frontier_graph.json"
CELLS = HERE / "data" / "cells.jsonl"
SYNAPSES = HERE / "data" / "synapses.jsonl"
EDGES = HERE / "data" / "edges.jsonl"
SHARD_DIR = ROOT / "site" / "assets" / "brain" / "cells"

AREA_RE = re.compile(r"^frontier:[A-Za-z][A-Za-z0-9_]{0,63}$")
KIND_MULT = {"depends": 3, "invocation": 3}   # the vote contract's 3x kinds
LAMBDA = 0.25                                 # the PROXIMITY CONTRACT damping
PROX_KEYS = ("db", "dw", "ib", "iw", "s", "r")
SUITABILITY_KEYS = ("candidate", "reason")
SUITABILITY_REASONS = {
    "existing_formal_coverage", "not_formalization_target", "broad_scope",
    "ambiguous_scope", "too_elementary", "review_needed", "no_concept_target",
}

# Pinned from the 2026-08-01 recon simulation (RECON FINDINGS + census script) —
# one cell per assignment tier. If the data drifts and a pin stops being
# homeless, the check SKIPS with a note rather than failing on stale ground.
PINS = [
    ("cell:Q1063054", "frontier:Analysis", "phase-1 vote (Morphism)"),
    ("cell:Q979829", "frontier:DeepFrontier_IntegralTransforms",
     "phase-2 MSC 44 (Radon transform)"),
    ("cell:Q1006428", "frontier:Geometry",
     "phase-3 relates hop (Two-body problem in general relativity)"),
]
UNSORTED_CEILING = 0.192 + 0.02   # recon-predicted share (309/1612) + 2pp

# PROXIMITY ground truth, measured on 2026-08-01 data (PROXIMITY CONTRACT):
# direct = cells with >=1 formalized-neighbor bond, bridged = cells whose only
# signal is a bridge through frontier neighbors, zero = no formal evidence
# within two hops. If the data drifts these MUST be re-measured (the F9
# failure detail says how) — the contract pins the exact counts against the
# current build.
PROX_PIN = {"direct": 852, "bridged": 451, "zero": 311}

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
    supercell_organs = cells_meta.get("supercell_organs", {})
    function_folded = any(o.get("id") == "Q11348" for organs in supercell_organs.values()
                          for o in organs)
    if function_folded:
        check("F2 Function is not a frontier cell",
              "cell:Q11348" not in set(seen),
              "Function should resolve to path:Mathlib/Logic/Function, not the frontier queue")
        bad_function_top = [(r["id"], t) for r in rows for t in r.get("top", [])
                            if t.get("cell") == "cell:Q11348"]
        check("F2 Function is not a frontier top target", not bad_function_top,
              f"found in top rows: {bad_function_top[:3]}")
    else:
        print("  SKIP F2 Function frontier regression — container_links not folded into cells")

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
    syn_rows = load_jsonl(SYNAPSES)[1]   # kept raw: F9/F10 re-derive from them
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

    # ---- F7: mean_stateability re-derives from current cells + synapses -----
    all_nb: dict[str, set[str]] = defaultdict(set)
    for synapse in syn_rows:
        src, dst = synapse.get("src", ""), synapse.get("dst", "")
        if src in cells and dst in cells:
            all_nb[src].add(dst)
            all_nb[dst].add(src)
    stateability_frac: dict[str, float] = {}
    for cid in homeless:
        if not any(o.get("kind") == "page" for o in cells[cid].get("organs", [])):
            continue
        neighbors = all_nb.get(cid, set())
        if neighbors:
            stateability_frac[cid] = round(
                sum(1 for other in neighbors if other in decl_cells) / len(neighbors),
                4,
            )
    bad_state = []
    for r in rows:
        fracs = [stateability_frac[c] for c in r["cells"] if c in stateability_frac]
        want = round(sum(fracs) / len(fracs), 4) if fracs else None
        if r["mean_stateability"] != want:
            bad_state.append((r["id"], r["mean_stateability"], want))
    check("F7 mean_stateability == current ring-1 all_frac mean (null when no join)",
          not bad_state, f"{bad_state[:3]}")
    inputs = meta.get("inputs", {})
    check("F7 metadata counts the derived stateability population and names no halo",
          inputs.get("stateability_joined") == len(stateability_frac)
          and "halo_joined" not in inputs,
          f"inputs={inputs}, derived={len(stateability_frac)}")

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
        stale_state = [
            r["id"] for r in rows if r["id"] in tree
            and tree[r["id"]].get("stateability") != r["mean_stateability"]
        ]
        check("F8 shard stateability matches frontier.jsonl exactly",
              not stale_state, f"{stale_state[:3]}")
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

    # ---- F9: FORMAL PROXIMITY — re-derived from the SPEC --------------------
    # PROXIMITY CONTRACT (brain/SCHEMA.md), never read from the builder:
    #   direct(c) = summed RAW weight of c's synapses into decl-organ cells
    #   bridge(c) = sum over frontier neighbors u of min(w(c,u), direct(u))
    #   s(c)      = direct(c) + bridge(c)/4      (exact — 1/4 is lossless)
    #   r(c)      = midrank percentile of s over ALL frontier cells, 4dp
    sdw: Counter = Counter()
    sdb: Counter = Counter()
    ff: dict[tuple[str, str], int] = {}
    for row in syn_rows:
        a, b, w = row["src"], row["dst"], row["weight"]
        if a in homeless and b in homeless:
            if a != b:
                k = (a, b) if a < b else (b, a)
                ff[k] = ff.get(k, 0) + w
            continue
        for x, y in ((a, b), (b, a)):
            if x in homeless and y in decl_cells:
                sdw[x] += w
                sdb[x] += 1
    ff_nbrs: dict[str, list] = defaultdict(list)
    for (a, b), w in ff.items():
        ff_nbrs[a].append((b, w))
        ff_nbrs[b].append((a, w))
    siw: dict[str, int] = {}
    sib: dict[str, int] = {}
    for c in homeless:
        iw = ib = 0
        for u, w in ff_nbrs.get(c, []):
            du = sdw.get(u, 0)
            if du:
                iw += min(w, du)
                ib += 1
        siw[c], sib[c] = iw, ib
    want_s = {c: sdw.get(c, 0) + siw[c] * LAMBDA for c in homeless}
    n_home = len(homeless)
    by_s = Counter(want_s.values())
    higher: dict[float, int] = {}
    acc = 0
    for v in sorted(by_s, reverse=True):
        higher[v] = acc
        acc += by_s[v]
    want_r = {c: round((higher[want_s[c]] + by_s[want_s[c]] / 2) / n_home, 4)
              for c in homeless}

    no_prox = [r["id"] for r in rows if not isinstance(r.get("prox"), dict)]
    check("F9 every area row carries a `prox` object", not no_prox,
          f"{no_prox[:3]}")
    proxed = [r for r in rows if isinstance(r.get("prox"), dict)]
    bad_keys = [(r["id"], sorted(r["prox"])) for r in proxed
                if set(r["prox"]) != set(PROX_KEYS)]
    check(f"F9 prox carries exactly the contract keys {PROX_KEYS}",
          not bad_keys, f"{bad_keys[:3]}")
    bad_len = [(r["id"], k) for r in proxed for k in r["prox"]
               if not isinstance(r["prox"][k], list)
               or len(r["prox"][k]) != r["n"]]
    check("F9 every prox array is parallel to the row's cells (len == n — "
          "every member scored exactly once)", not bad_len, f"{bad_len[:3]}")
    proxed = [r for r in proxed
              if set(r["prox"]) == set(PROX_KEYS)
              and all(isinstance(r["prox"][k], list)
                      and len(r["prox"][k]) == r["n"] for k in PROX_KEYS)]
    bad_val = []
    for r in proxed:
        p = r["prox"]
        for i in range(r["n"]):
            if not all(isinstance(p[k][i], int) and p[k][i] >= 0
                       for k in ("db", "dw", "ib", "iw")) \
                    or not all(isinstance(p[k][i], (int, float))
                               and not math.isnan(p[k][i]) and p[k][i] >= 0
                               for k in ("s", "r")) \
                    or p["r"][i] > 1:
                bad_val.append((r["id"], r["cells"][i]))
    check("F9 no NaN, nothing negative: db/dw/ib/iw ints >= 0, s >= 0, "
          "r in [0, 1]", not bad_val, f"{len(bad_val)}, e.g. {bad_val[:3]}")
    # per-cell agreement with the spec — every cell, every field, not a sample
    shipped: dict[str, tuple] = {}
    for r in proxed:
        p = r["prox"]
        for i, c in enumerate(r["cells"]):
            shipped[c] = (p["db"][i], p["dw"][i], p["ib"][i], p["iw"][i],
                          p["s"][i], p["r"][i])
    div = [(c, shipped[c],
            (sdb.get(c, 0), sdw.get(c, 0), sib[c], siw[c], want_s[c],
             want_r[c]))
           for c in shipped
           if shipped[c] != (sdb.get(c, 0), sdw.get(c, 0), sib[c], siw[c],
                             want_s[c], want_r[c])]
    check(f"F9 every cell's (db, dw, ib, iw, s, r) matches the spec "
          f"({len(shipped)} cells checked, exact equality)", not div,
          f"{len(div)} diverge (cell, shipped, spec), e.g. {div[:2]}")
    check("F9 s == dw + iw/4 EXACTLY on every shipped cell (the one-sentence "
          "formula holds)",
          all(v[4] == v[1] + v[3] * LAMBDA for v in shipped.values()))
    # _meta.proximity: the accounting the tooltips and the UI legend read
    pmeta = meta.get("proximity") or {}
    check("F9 _meta.proximity names the method (min-capped bridge) and lambda",
          "min(" in (pmeta.get("method") or "") and pmeta.get("lambda") == LAMBDA,
          f"method={pmeta.get('method')!r} lambda={pmeta.get('lambda')!r}")
    got_counts = {
        "direct": sum(1 for c in homeless if sdw.get(c, 0) > 0),
        "bridged": sum(1 for c in homeless
                       if sdw.get(c, 0) == 0 and siw[c] > 0),
    }
    got_counts["zero"] = n_home - got_counts["direct"] - got_counts["bridged"]
    check("F9 _meta.proximity.counts == the spec recount",
          pmeta.get("counts") == got_counts,
          f"_meta says {pmeta.get('counts')}, spec says {got_counts}")
    check(f"F9 ground truth holds: counts == {PROX_PIN} (direct/bridged/zero)",
          got_counts == PROX_PIN,
          f"spec recount says {got_counts} — if brain/data legitimately "
          f"drifted, re-measure and update PROX_PIN (and the PROXIMITY "
          f"CONTRACT counts in brain/SCHEMA.md)")
    # MONOTONICITY on the real data, full pairwise: strictly more direct
    # weight at >= equal bridge weight must mean a strictly higher score.
    vals = sorted((sdw.get(c, 0), siw[c], want_s[c]) for c in homeless)
    mono_bad = 0
    for i in range(len(vals)):
        dwi, iwi, si = vals[i]
        for j in range(i + 1, len(vals)):
            dwj, iwj, sj = vals[j]
            if dwj > dwi and iwj >= iwi and sj <= si:
                mono_bad += 1
    check(f"F9 MONOTONE on the data: dw_a > dw_b and iw_a >= iw_b => "
          f"s_a > s_b ({len(vals)} cells, full pairwise)", mono_bad == 0,
          f"{mono_bad} violating pairs")
    # JACK REGRESSION (the reason hop tiering died): pick, deterministically
    # from the spec values, (a) the most-bonded cell, (b) a weakest-direct
    # cell (one bond, weight 1), (c) a cell whose ONLY signal is one bridge
    # through one near-isolated intermediary. Hop tiering called (a) and (b)
    # the same tier and ranked (c) right behind them; the score must spread
    # them by orders of magnitude.
    cand_a = max(homeless, key=lambda c: (sdb.get(c, 0), c))
    cand_b = min((c for c in homeless
                  if sdb.get(c) == 1 and sdw.get(c) == 1),
                 key=lambda c: (siw[c], c), default=None)
    cand_c = min((c for c in homeless
                  if sdw.get(c, 0) == 0 and sib[c] == 1 and 0 < siw[c] <= 3),
                 key=lambda c: (siw[c], c), default=None)
    if sdb.get(cand_a, 0) < 100 or cand_b is None or cand_c is None:
        print("  SKIP F9 Jack regression — the data no longer holds the "
              "pattern (re-pick candidates)")
    else:
        la = cells[cand_a].get("label")
        lb = cells[cand_b].get("label")
        lc = cells[cand_c].get("label")
        check(f"F9 JACK REGRESSION: {la!r} ({sdb[cand_a]} direct bonds) "
              f"scores >= 100x {lb!r} (1 direct bond, weight 1)",
              want_s[cand_a] >= 100 * want_s[cand_b],
              f"{want_s[cand_a]} vs {want_s[cand_b]}")
        check(f"F9 JACK REGRESSION: {lb!r} (1 direct bond) outscores {lc!r} "
              f"(only path = 1 bridge through a near-isolated intermediary, "
              f"s <= {3 * LAMBDA})",
              want_s[cand_b] > want_s[cand_c]
              and want_s[cand_c] <= 3 * LAMBDA,
              f"{want_s[cand_b]} vs {want_s[cand_c]}")
        check("F9 JACK REGRESSION: the radii order the same way "
              "(r_a < r_b < r_c — closer to the core = smaller radius)",
              want_r[cand_a] < want_r[cand_b] < want_r[cand_c],
              f"{want_r[cand_a]} / {want_r[cand_b]} / {want_r[cand_c]}")
    # a cell with >= 100 direct weight must outscore EVERY zero-direct cell
    # (measured margin on 2026-08-01 data: 110.0 vs 2.5)
    heavy = [c for c in homeless if sdw.get(c, 0) >= 100]
    no_direct_max = max((want_s[c] for c in homeless if sdw.get(c, 0) == 0),
                        default=0)
    check(f"F9 every cell with direct weight >= 100 ({len(heavy)}) outscores "
          f"every zero-direct cell (max bridge-only score {no_direct_max})",
          all(want_s[c] > no_direct_max for c in heavy),
          f"min heavy score {min((want_s[c] for c in heavy), default=0)}")

    # ---- F10: FRONTIER GRAPH — the client-side re-scoring input -------------
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

        # formal: keys are canonical exact root SETS of real library roots;
        # weights are positive ints
        lib_universe = {sup.split(":", 1)[1].split("/")[0]
                        for c in cell_rows for sup in c.get("supercells") or []}
        fallback_libs = {o["id"].split(":", 2)[1]
                         for c in cell_rows
                         if c["id"] in decl_cells
                         and not (c.get("supercells") or [])
                         for o in c["organs"] if o.get("kind") == "decl"}
        valid_libs = lib_universe | fallback_libs
        bad_key = [(cid, key) for cid, lrow in gformal.items()
                   for key in lrow
                   if key != "|".join(sorted(key.split("|")))
                   or any(root not in valid_libs for root in key.split("|"))]
        check(f"F10 every formal key is a canonical '|'-joined sorted set of "
              f"real library roots (universe: {sorted(valid_libs)})",
              not bad_key, f"{len(bad_key)} bad, e.g. {bad_key[:3]}")
        bad_w = [(cid, key, w) for cid, lrow in gformal.items()
                 for key, w in lrow.items()
                 if not isinstance(w, int) or w <= 0]
        check("F10 every formal weight is a positive int", not bad_w,
              f"{bad_w[:3]}")

        # formal + edges, re-derived from the SPEC over the raw synapse rows
        def libs_of_formal(cid: str) -> list[str]:
            libs = {sup.split(":", 1)[1].split("/")[0]
                    for sup in cells[cid].get("supercells") or []}
            if not libs:   # supercell-less decl cell: the decl id's <Lib> segment
                libs = {o["id"].split(":", 2)[1] for o in cells[cid]["organs"]
                        if o.get("kind") == "decl"}
            return sorted(libs)

        gindex = {c: i for i, c in enumerate(gcells)}
        want_formal: dict[str, Counter] = defaultdict(Counter)
        want_edges: dict[tuple[int, int], int] = {}
        for row in syn_rows:
            a, b = row["src"], row["dst"]
            if a in homeless and b in homeless:
                i, j = gindex.get(a), gindex.get(b)
                if i is not None and j is not None and i != j:
                    k = (i, j) if i < j else (j, i)
                    want_edges[k] = want_edges.get(k, 0) + row["weight"]
                continue
            for x, y in ((a, b), (b, a)):
                if x in homeless and y in decl_cells:
                    want_formal[x]["|".join(libs_of_formal(y))] += row["weight"]
        div_formal = [cid for cid in set(want_formal) | set(gformal)
                      if dict(want_formal.get(cid) or {}) != gformal.get(cid)]
        div_detail = [(c, gformal.get(c), dict(want_formal.get(c) or {}))
                      for c in div_formal[:2]]
        check(f"F10 `formal` re-derives from the spec on all "
              f"{len(gformal)} cells (exact-root-set summed RAW weight)",
              not div_formal,
              f"{len(div_formal)} diverge (cell, shipped, spec), "
              f"e.g. {div_detail}")

        n_cells = len(gcells)
        bad_e = [e for e in gedges
                 if len(e) != 3 or not (0 <= e[0] < n_cells
                                        and 0 <= e[1] < n_cells)
                 or e[0] >= e[1] or not isinstance(e[2], int) or e[2] <= 0]
        check("F10 edges are in-range [i, j, w] triples with i < j and a "
              "positive int RAW weight", not bad_e,
              f"{len(bad_e)} bad, e.g. {bad_e[:3]}")
        etup = [tuple(e) for e in gedges]
        pair_list = [(e[0], e[1]) for e in etup]
        check("F10 edges are sorted + deduped (deterministic bytes)",
              pair_list == sorted(set(pair_list)),
              f"{len(pair_list) - len(set(pair_list))} dupes / out of order")
        check(f"F10 edges == every frontier<->frontier synapse with its RAW "
              f"weight ({len(want_edges)} pairs from synapses.jsonl)",
              {(i, j): w for i, j, w in etup} == want_edges,
              f"{len(set(pair_list) - set(want_edges))} extra, "
              f"{len(set(want_edges) - set(pair_list))} missing, "
              f"{sum(1 for i, j, w in etup if want_edges.get((i, j)) not in (None, w))} "
              f"wrong weight")

        # THE PARITY LAW, proved on the emitted bytes: the client re-score
        # (spec: direct_L = sum of formal entries whose root set intersects L;
        # score_L = direct_L + sum(min(w, direct_L(u)))/4 over edges) must
        # reproduce the SHIPPED s EXACTLY with all libraries enabled, and must
        # equal an INDEPENDENT raw-synapse restriction under proper subsets.
        adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for i, j, w in etup:
            adj[i].append((j, w))
            adj[j].append((i, w))

        def graph_scores(enabled: set[str] | None) -> dict[str, float]:
            dl: dict[str, int] = {}
            for cid, lrow in gformal.items():
                tot = sum(w for key, w in lrow.items()
                          if enabled is None
                          or not enabled.isdisjoint(key.split("|")))
                if tot:
                    dl[cid] = tot
            out = {}
            for c in gcells:
                acc2 = 0
                for jn, w in adj.get(gindex[c], []):
                    du = dl.get(gcells[jn], 0)
                    if du:
                        acc2 += min(w, du)
                out[c] = dl.get(c, 0) + acc2 * LAMBDA
            return out

        all_scores = graph_scores(None)
        parity_div = [(c, all_scores[c], shipped.get(c))
                      for c in gcells
                      if c not in shipped or all_scores[c] != shipped[c][4]]
        check(f"F10 PARITY LAW: the all-libraries client re-score over the "
              f"graph == the shipped s on ALL {len(gcells)} cells (exact "
              f"float equality)", not parity_div,
              f"{len(parity_div)} diverge (cell, client, shipped), "
              f"e.g. {parity_div[:3]}")
        # subsets, against an independent raw-synapse restriction: a neighbor
        # counts iff its OWN root set intersects L (exact sets — a multi-root
        # neighbor is never double-counted, never half-counted)
        subset_bad = []
        for L in ({"Mathlib"}, {"Init"}, {"FormalConjectures"}):
            gs = graph_scores(L)
            rdw: Counter = Counter()
            for row in syn_rows:
                a, b = row["src"], row["dst"]
                for x, y in ((a, b), (b, a)):
                    if x in homeless and y in decl_cells \
                            and not L.isdisjoint(libs_of_formal(y)):
                        rdw[x] += row["weight"]
            for c in gcells:
                acc2 = 0
                for u, w in ff_nbrs.get(c, []):
                    du = rdw.get(u, 0)
                    if du:
                        acc2 += min(w, du)
                if gs[c] != rdw.get(c, 0) + acc2 * LAMBDA:
                    subset_bad.append((sorted(L), c))
        check("F10 PARITY under library subsets: graph re-score == an "
              "independent raw-synapse restriction (Mathlib / Init / "
              "FormalConjectures, all cells)", not subset_bad,
              f"{len(subset_bad)} diverge, e.g. {subset_bad[:3]}")
        # per-root adjacency accounting in _meta (the Libraries UI's numbers)
        want_libs: Counter = Counter()
        for lrow in gformal.values():
            for root in {root for key in lrow for root in key.split("|")}:
                want_libs[root] += 1
        check("F10 _meta.counts.libs == per-root adjacent-cell recount "
              "(each cell once per root)",
              gcounts.get("libs") == dict(sorted(want_libs.items())),
              f"_meta says {gcounts.get('libs')}, spec says "
              f"{dict(sorted(want_libs.items()))}")

    # ---- F11: SUITABILITY is aligned queue metadata, never membership --------
    cell_suitability = {}
    bad_suitability = []
    for row in rows:
        su = row.get("suitability")
        if not isinstance(su, dict) or set(su) != set(SUITABILITY_KEYS):
            bad_suitability.append((row["id"], "keys"))
            continue
        if any(not isinstance(su[k], list) or len(su[k]) != row["n"]
               for k in SUITABILITY_KEYS):
            bad_suitability.append((row["id"], "alignment"))
            continue
        for i, cid in enumerate(row["cells"]):
            candidate, reason = su["candidate"][i], su["reason"][i]
            if not isinstance(candidate, bool) or (candidate and reason is not None) \
                    or (not candidate and reason not in SUITABILITY_REASONS):
                bad_suitability.append((cid, candidate, reason))
            cell_suitability[cid] = (candidate, reason)
    check("F11 every area carries aligned candidate/reason suitability arrays",
          not bad_suitability, f"{bad_suitability[:3]}")
    check("F11 suitability classifies the complete structural Frontier",
          set(cell_suitability) == homeless,
          f"missing={len(homeless - set(cell_suitability))}, "
          f"extra={len(set(cell_suitability) - homeless)}")
    suitability_meta = meta.get("suitability", {}).get("counts", {})
    candidate_n = sum(1 for candidate, _ in cell_suitability.values() if candidate)
    reason_counts = Counter(reason for candidate, reason in cell_suitability.values()
                            if not candidate)
    check("F11 suitability metadata counts reconcile",
          suitability_meta.get("candidate") == candidate_n
          and suitability_meta.get("deprioritized") == len(homeless) - candidate_n
          and suitability_meta.get("reasons") == dict(sorted(reason_counts.items())),
          f"meta={suitability_meta}, candidates={candidate_n}, reasons={reason_counts}")

    reported = {
        "cell:Q854531", "cell:Q12485", "cell:Q185264", "cell:Q44528",
        "cell:Q837863", "cell:Q172891", "cell:Q33456", "cell:Q200227",
        "cell:Q901718", "cell:Q44946",
    }
    missing_reported = sorted(reported - homeless)
    still_candidates = sorted(c for c in reported
                              if cell_suitability.get(c, (True, None))[0])
    check("F11 the reported top ten remain present but are all deprioritized",
          not missing_reported and not still_candidates,
          f"missing={missing_reported}, still candidates={still_candidates}")
    expected_reasons = {
        "cell:Q854531": "existing_formal_coverage",
        "cell:Q12485": "existing_formal_coverage",
        "cell:Q185264": "broad_scope",
        "cell:Q44528": "existing_formal_coverage",
        "cell:Q837863": "broad_scope",
        "cell:Q172891": "not_formalization_target",
        "cell:Q33456": "review_needed",
        "cell:Q200227": "existing_formal_coverage",
        "cell:Q901718": "existing_formal_coverage",
        "cell:Q44946": "ambiguous_scope",
    }
    wrong_reasons = [(cid, cell_suitability.get(cid), want)
                     for cid, want in expected_reasons.items()
                     if cell_suitability.get(cid) != (False, want)]
    check("F11 the reported top ten carry evidence-backed review reasons",
          not wrong_reasons, f"{wrong_reasons}")
    check("F11 actionable candidates remain after policy classification",
          candidate_n > 0, "every Frontier cell was deprioritized")
    representative_candidates = {
        "cell:Q864145",   # Split-complex number
        "cell:Q203218",   # Spherical coordinate system
        "cell:Q576072",   # Student's t-distribution
        "cell:Q165498",   # Schrödinger equation
        "cell:Q751290",   # Euler angles
        "cell:Q753035",   # Riemann surface
    }
    lost_candidates = sorted(c for c in representative_candidates
                             if cell_suitability.get(c) != (True, None))
    check("F11 representative bounded gaps remain actionable",
          not lost_candidates, f"deprioritized unexpectedly: {lost_candidates}")

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
