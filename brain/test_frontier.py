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

Run: python3 brain/test_frontier.py
     (after brain/build_frontier.py + brain/build_cell_shards.py)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRONTIER = HERE / "data" / "frontier.jsonl"
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
    for row in load_jsonl(SYNAPSES)[1]:
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
    proc = subprocess.run([sys.executable, str(HERE / "build_frontier.py")],
                          capture_output=True, text=True)
    check("F6 a rebuild exits 0", proc.returncode == 0,
          (proc.stderr or proc.stdout)[-300:])
    check("F6 two runs are byte-identical (deterministic, seedless)",
          FRONTIER.read_bytes() == before)

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
