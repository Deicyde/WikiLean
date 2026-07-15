#!/usr/bin/env python3
"""Build-time force layout for BRAIN v3 cells.

The v2 client ran a d3-force simulation over ~5.7k nodes on every visit: it froze
the tab for seconds, forced a 4,000-edge draw cap (which caused the phantom-ring
bug), and drew a DIFFERENT map every time, so the picture could never be learned.
v3 runs the simulation HERE, once, and ships `xy` per cell. The client renders and
never simulates.

Deterministic by construction — phyllotaxis seeding, no RNG, fixed iteration count
— so a rebuild with unchanged inputs reproduces the same map byte-for-byte.

## Why repulsion is short-range (the "ring + centre clump" bug)

Textbook Fruchterman-Reingold repels EVERY pair at k^2/d, which is long-range: a
node feels ~n*k^2/d pulling it outward, so a weakly-attached node only stops where
that balances gravity, at r = sqrt(n*k^2/g). Measured on this graph that is 86,516
— and the layout put its 488 isolated cells at r~84,200 while the 8,494 real cells
sat at r~1,985. Fit-to-content then zooms out 42x and the graph becomes a dot
inside a giant ring. That is exactly the artefact reported against the v2 explorer
(d3-force's charge is long-range by default too).

So repulsion is cut off at REPULSION_RANGE: beyond it nodes stop pushing, gravity
wins, and the layout settles into a compact disc whose spacing is set by LOCAL
density. Isolated cells never enter the sim at all — see place_isolated().
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

CHUNK = 256               # repulsion rows per pass — caps peak memory at ~40MB
EPS = 1e-9
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

SPAN = 100.0              # ideal edge length k — the map's unit of distance
REPULSION_RANGE = 4.0     # in units of k; the fix above. Long-range => halo.
GRAVITY = 0.02            # only has to beat short-range repulsion now


def _phyllotaxis(n: int, radius: float, *, offset: int = 0) -> np.ndarray:
    """Sunflower seeding: uniform density, no clumps, no RNG."""
    i = np.arange(offset, offset + n, dtype=np.float64)
    theta = i * GOLDEN_ANGLE
    r = radius * np.sqrt((i - offset + 0.5) / max(n, 1))
    return np.stack([r * np.cos(theta), r * np.sin(theta)], axis=1)


def _simulate(pos: np.ndarray, src: np.ndarray, dst: np.ndarray, weight: np.ndarray,
              *, iterations: int, k: float) -> np.ndarray:
    n = len(pos)
    cutoff2 = (REPULSION_RANGE * k) ** 2
    temp = k * 3.0
    cooling = temp / (iterations + 1)

    for _ in range(iterations):
        disp = np.zeros((n, 2), dtype=np.float64)

        # repulsion f = k^2/d within REPULSION_RANGE, zero beyond (see module docstring)
        for start in range(0, n, CHUNK):
            end = min(start + CHUNK, n)
            delta = pos[start:end, None, :] - pos[None, :, :]      # (c, n, 2)
            d2 = np.einsum("ijk,ijk->ij", delta, delta)            # (c, n)
            np.maximum(d2, EPS, out=d2)
            coef = np.where(d2 < cutoff2, (k * k) / d2, 0.0)
            rows = np.arange(end - start)
            coef[rows, rows + start] = 0.0                         # no self-repulsion
            disp[start:end] = np.einsum("ijk,ij->ik", delta, coef)

        # attraction along synapses: f = d^2/k, scaled by log weight
        if src.size:
            delta = pos[src] - pos[dst]
            dist = np.sqrt(np.einsum("ij,ij->i", delta, delta))
            np.maximum(dist, EPS, out=dist)
            pull = delta * (dist * weight / k)[:, None]
            np.add.at(disp, src, -pull)
            np.add.at(disp, dst, pull)

        disp -= pos * GRAVITY

        length = np.sqrt(np.einsum("ij,ij->i", disp, disp))
        np.maximum(length, EPS, out=length)
        pos += disp * (np.minimum(length, temp) / length)[:, None]
        temp -= cooling

    return pos


def place_isolated(cells: dict[str, dict], isolated: list[str],
                   pos_by_id: dict[str, tuple[float, float]], core_radius: float) -> None:
    """Give synapse-less cells a deterministic home instead of a free body.

    A cell with no synapses has nothing to hold it: in the sim it would either fly
    into the halo or pile onto the origin — both of which read as the "clump" bug.
    But it is not meaningless, so it is not dropped: a lone decl still lives in a
    module, and locality is the point of the map. So it is parked in a tight
    phyllotaxis around its supercell's centre of mass; a cell with no supercell
    either (no decl, no synapse) goes to a tidy outer band.
    """
    by_super: dict[str, list[str]] = defaultdict(list)
    homeless: list[str] = []
    for cid in isolated:
        sups = cells[cid].get("supercells") or []
        (by_super[sups[0]].append(cid) if sups else homeless.append(cid))

    # centroid of each supercell's already-placed (connected) cells
    centroid: dict[str, np.ndarray] = {}
    members: dict[str, list[np.ndarray]] = defaultdict(list)
    for cid, cell in cells.items():
        p = pos_by_id.get(cid)
        if p is None:
            continue
        for sup in cell.get("supercells") or []:
            members[sup].append(np.asarray(p))
    for sup, pts in members.items():
        centroid[sup] = np.mean(pts, axis=0)

    for sup, group in sorted(by_super.items()):
        home = centroid.get(sup)
        ring = _phyllotaxis(len(group), SPAN * 1.5)
        if home is None:  # supercell has no connected cell to anchor to
            homeless.extend(group)
            continue
        for cid, offset in zip(sorted(group), ring):
            pos_by_id[cid] = tuple(home + offset)

    if homeless:
        band = _phyllotaxis(len(homeless), core_radius * 0.18)
        band = band + np.array([core_radius * 1.15, 0.0])
        for cid, p in zip(sorted(homeless), band):
            pos_by_id[cid] = tuple(p)


def layout_cells(cells: dict[str, dict], synapses: list[dict], *,
                 iterations: int = 200) -> None:
    """Compute `xy` for every cell (mutates `cells`)."""
    ids = sorted(cells)
    if not ids:
        return
    if len(ids) == 1:
        cells[ids[0]]["xy"] = [0.0, 0.0]
        return

    degree: dict[str, int] = defaultdict(int)
    for syn in synapses:
        degree[syn["src"]] += 1
        degree[syn["dst"]] += 1
    connected = [c for c in ids if degree[c]]
    isolated = [c for c in ids if not degree[c]]
    if not connected:  # nothing to simulate; everything is a lone particle
        connected, isolated = ids, []

    index = {cid: i for i, cid in enumerate(connected)}
    n = len(connected)
    k = SPAN
    pos = _phyllotaxis(n, SPAN * math.sqrt(n) / 2.0)

    src_l, dst_l, w_l = [], [], []
    for syn in synapses:
        a, b = index.get(syn["src"]), index.get(syn["dst"])
        if a is None or b is None or a == b:
            continue
        src_l.append(a)
        dst_l.append(b)
        # log-damped: a 400-trace synapse should pull harder than a 1-trace one,
        # but not 400x harder — that collapses hubs to a point.
        w_l.append(math.log1p(syn.get("weight", 1)))
    src = np.asarray(src_l, dtype=np.int64)
    dst = np.asarray(dst_l, dtype=np.int64)
    weight = np.asarray(w_l, dtype=np.float64)

    pos = _simulate(pos, src, dst, weight, iterations=iterations, k=k)
    pos -= pos.mean(axis=0)

    pos_by_id: dict[str, tuple[float, float]] = {
        cid: (float(x), float(y)) for cid, (x, y) in zip(connected, pos)
    }
    core_radius = float(np.percentile(np.hypot(pos[:, 0], pos[:, 1]), 98)) or SPAN
    place_isolated(cells, isolated, pos_by_id, core_radius)

    for cid in ids:
        x, y = pos_by_id[cid]
        cells[cid]["xy"] = [round(x, 1), round(y, 1)]
