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

## Big outside libraries sit in module bubbles around the core

The sim cannot hold all of TauCeti. With its 311k lone-decl cells in, attraction to
the Mathlib hubs piled them together and the grid's candidate pairs grew from 28M to
872M over the first 50 steps. So only the CORE is simulated: every cell except the
lone decl cells (`cell:decl:<Lib>:<name>`) of a library outside SIMULATED_DECL_LIBS.
The core goes through the path above with those cells and their synapses removed,
so its positions are exactly the layout of the core alone.

Each module (a member's deepest supercell, or `path:<Lib>` if it has none) becomes
a disc of radius SPAN/2 * sqrt(members), anchored at the synapse-weighted mean of
the core cells its members bond with; a module with no such bond borrows its nearest
anchored ancestor's anchor, else the mean anchor. Discs start just outside the core
(its farthest cell, so the band place_isolated() puts outside it is covered too) in
their anchor's direction, relax for
BUBBLE_ROUNDS rounds (overlap push, pull toward the anchor, pushed back out of the
core), and then an exact pass moves each disc, largest first, outward along its ray
from the origin until it clears every disc placed before it. So no two discs
overlap and none enters the core, with 5% of a disc's radius to spare (BUBBLE_GAP).
Members spiral inside their disc, ordered by id. See place_bubbles(). layout_cells
returns the discs, which build_cells records in the cells.jsonl `_meta` as `bubbles`;
bubble_module is the membership rule, shared with build_serving_db, which draws a module
as one dot in an explorer scope too big to draw cell by cell.
"""
from __future__ import annotations

import math
from collections import defaultdict

try:
    import numpy as np
except ModuleNotFoundError:     # bubble_module needs none, and build_serving_db imports it on
    np = None                   # hosts without numpy (CI does not pin it); layout_cells refuses

PAIR_BUDGET = 1 << 19     # candidate pairs per repulsion pass — caps peak memory near 70MB
EPS = 1e-9
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

SPAN = 100.0              # ideal edge length k — the map's unit of distance
REPULSION_RANGE = 4.0     # in units of k; the fix above. Long-range => halo.
GRAVITY = 0.02            # only has to beat short-range repulsion now

# lone decl cells of every other library go into module bubbles, not the sim
SIMULATED_DECL_LIBS = frozenset({"Mathlib", "Init", "Batteries", "FormalConjectures"})
BUBBLE_ROUNDS = 400       # relaxation rounds before the exact no-overlap pass
BUBBLE_PULL = 0.02        # fraction of the way back toward its anchor per round
BUBBLE_GAP = 1.05         # a disc keeps 5% of its radius clear of discs and the core


def _phyllotaxis(n: int, radius: float, *, skip_centre: bool = False) -> np.ndarray:
    """Sunflower seeding: uniform density, no clumps, no RNG.

    skip_centre starts the spiral at index 1, so no point lands on the anchor itself
    — needed when the anchor is an occupied position (a supercell centroid).
    """
    if n <= 0:
        return np.zeros((0, 2), dtype=np.float64)
    base = 1 if skip_centre else 0
    i = np.arange(base, base + n, dtype=np.float64)
    theta = i * GOLDEN_ANGLE
    r = radius * np.sqrt((i + 0.5) / (n + base))
    return np.stack([r * np.cos(theta), r * np.sin(theta)], axis=1)


def _resolve_collisions(pos_by_id: dict[str, tuple[float, float]],
                        fixed: set[str] | frozenset[str] = frozenset()) -> int:
    """Nudge cells that share a position onto a tiny golden-angle spiral.

    Two cells at identical coordinates render as one — the second is unclickable and
    simply invisible. The placement rules make this rare but not impossible: a
    supercell with exactly ONE connected cell has a centroid equal to that cell's
    position, so its lone isolated member lands exactly on top of it (20 cells on the
    live graph). Deterministic: groups and offsets are ordered by id.

    Cells in `fixed` keep their spot, so a second pass for the bubble members never
    moves a core cell the first pass placed.
    """
    seen: dict[tuple[float, float], list[str]] = defaultdict(list)
    for cid, p in pos_by_id.items():
        seen[(round(p[0], 3), round(p[1], 3))].append(cid)
    nudged = 0
    for point, ids in sorted(seen.items()):
        if len(ids) < 2:
            continue
        ids = sorted(ids, key=lambda c: (c not in fixed, c))
        stay = sum(c in fixed for c in ids) or 1    # else the first keeps the spot
        ring = _phyllotaxis(len(ids) - stay, SPAN * 0.35, skip_centre=True)
        for cid, off in zip(ids[stay:], ring):
            pos_by_id[cid] = (point[0] + float(off[0]), point[1] + float(off[1]))
            nudged += 1
    return nudged


def _repulsion(pos: np.ndarray, k: float, *, budget: int = PAIR_BUDGET) -> np.ndarray:
    """Repulsion f = k^2/d within REPULSION_RANGE, zero beyond (see module docstring).

    Linear in n at bounded density. Nodes are bucketed on a grid one cutoff wide, so
    every nonzero term lies in a node's own bucket or one of its 8 neighbours. Nodes
    are sorted by bucket key (stable, so ties keep index order), which makes the three
    buckets of each neighbouring column one contiguous run of the sorted order. Each
    candidate pair gets the all-pairs arithmetic (delta, d2, EPS floor, strict cutoff),
    so only the summation order differs from comparing every pair. A node's pairs never
    straddle two passes, so the result does not depend on `budget`.
    """
    n = len(pos)
    disp = np.zeros((n, 2), dtype=np.float64)
    if n == 0:
        return disp
    cutoff2 = (REPULSION_RANGE * k) ** 2
    # 0.1% wider than the cutoff, so rounding cannot put an in-range pair two buckets apart
    size = REPULSION_RANGE * k * 1.001
    cell = np.floor((pos - pos.min(axis=0)) / size).astype(np.int64)
    stride = int(cell[:, 1].max()) + 3          # y +-1 never wraps into the next column
    key = (cell[:, 0] + 1) * stride + (cell[:, 1] + 1)
    order = np.argsort(key, kind="stable")
    skey = key[order]
    spos = pos[order]

    # per node, where the x-1, x and x+1 columns (3 buckets each) start in sorted order
    # and how many nodes each holds
    column = skey[:, None] + np.array([-stride, 0, stride], dtype=np.int64)
    lo = np.searchsorted(skey, column - 1, side="left")
    run = np.searchsorted(skey, column + 1, side="right") - lo
    per_node = run.sum(axis=1)
    reach = np.cumsum(per_node)

    sdisp = np.empty((n, 2), dtype=np.float64)
    start = 0
    while start < n:
        done = int(reach[start - 1]) if start else 0
        end = max(int(np.searchsorted(reach, done + budget, side="right")), start + 1)
        runs = run[start:end].ravel()
        counts = per_node[start:end]
        # j lists each node's three runs back to back: run r is lo_r .. lo_r + runs_r - 1
        shift = lo[start:end].ravel() - (np.cumsum(runs) - runs)
        j = np.arange(int(counts.sum())) + np.repeat(shift, runs)
        delta = np.repeat(spos[start:end], counts, axis=0) - spos[j]
        d2 = np.einsum("ij,ij->i", delta, delta)
        np.maximum(d2, EPS, out=d2)
        coef = np.where(d2 < cutoff2, (k * k) / d2, 0.0)   # self-pairs have delta = 0
        owner = np.repeat(np.arange(end - start), counts)
        sdisp[start:end, 0] = np.bincount(owner, weights=delta[:, 0] * coef, minlength=end - start)
        sdisp[start:end, 1] = np.bincount(owner, weights=delta[:, 1] * coef, minlength=end - start)
        start = end
    disp[order] = sdisp
    return disp


def _simulate(pos: np.ndarray, src: np.ndarray, dst: np.ndarray, weight: np.ndarray,
              *, iterations: int, k: float) -> np.ndarray:
    temp = k * 3.0
    cooling = temp / (iterations + 1)

    for _ in range(iterations):
        disp = _repulsion(pos, k)

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
        if home is None:  # supercell has no connected cell to anchor to
            homeless.extend(group)
            continue
        # skip_centre: the centroid may BE an occupied position (a supercell with one
        # connected cell), so no isolated member may sit exactly on it
        ring = _phyllotaxis(len(group), SPAN * 1.5, skip_centre=True)
        for cid, offset in zip(sorted(group), ring):
            pos_by_id[cid] = (float(home[0] + offset[0]), float(home[1] + offset[1]))

    if homeless:
        band = _phyllotaxis(len(homeless), core_radius * 0.18)
        band = band + np.array([core_radius * 1.15, 0.0])
        for cid, p in zip(sorted(homeless), band):
            pos_by_id[cid] = (float(p[0]), float(p[1]))


def _bubble_library(cid: str) -> str | None:
    """The library of a decl cell whose library goes into module bubbles, else None.

    A decl cell's id is `cell:` + its decl id `decl:<Lib>:<name>`. layout_cells also
    requires the cell to be lone (no organ besides its decl).
    """
    parts = cid.split(":", 3)
    if len(parts) == 4 and parts[:2] == ["cell", "decl"] and parts[2] not in SIMULATED_DECL_LIBS:
        return parts[2]
    return None


def bubble_module(cid: str, supercells: list[str] | None, organs: int) -> str | None:
    """The module whose disc holds cell `cid`, or None when the core simulates it.

    Only a lone decl (at most `organs` = 1: a decl cell that also holds a statement or
    concept stays simulated) of a library outside SIMULATED_DECL_LIBS is bubbled; its
    module is its deepest supercell, else `path:<Lib>`. Needs no numpy: build_serving_db
    applies the same rule to the shard entries (id, supercells, organ count).
    """
    lib = _bubble_library(cid)
    if lib is None or organs > 1:
        return None
    return min(supercells or [f"path:{lib}"], key=lambda s: (-s.count("/"), s))  # the deepest


def _unit(v: np.ndarray) -> np.ndarray:
    """Row-wise unit vectors; the origin has no direction, so it gets +x."""
    length = np.hypot(v[:, 0], v[:, 1])
    out = np.zeros_like(v)
    out[:, 0] = 1.0
    ok = length > EPS
    out[ok] = v[ok] / length[ok, None]
    return out


def _relax_discs(pos: np.ndarray, anchor: np.ndarray, pad: np.ndarray,
                 floor: np.ndarray) -> np.ndarray:
    """BUBBLE_ROUNDS of: push overlapping discs apart, pull toward the anchor, and put
    any disc that entered the core back on its edge. Gets the discs close; it does not
    guarantee anything, _clear_discs does."""
    n = len(pos)
    step = max(1, PAIR_BUDGET // n)
    for _ in range(BUBBLE_ROUNDS):
        move = np.zeros_like(pos)
        for start in range(0, n, step):
            end = min(start + step, n)
            delta = pos[start:end, None, :] - pos[None, :, :]
            dist = np.sqrt(np.einsum("ijk,ijk->ij", delta, delta))
            need = pad[start:end, None] + pad[None, :]
            over = np.where(dist < need, need - dist, 0.0)
            over[np.arange(end - start), np.arange(start, end)] = 0.0
            np.maximum(dist, EPS, out=dist)
            move[start:end] = np.einsum("ijk,ij->ik", delta, over / dist) * 0.5
        pos = pos + move + BUBBLE_PULL * (anchor - pos)
        low = np.hypot(pos[:, 0], pos[:, 1]) < floor
        pos[low] = _unit(pos[low]) * floor[low, None]
    return pos


def _clear_discs(pos: np.ndarray, pad: np.ndarray, floor: np.ndarray,
                 order: list[int]) -> np.ndarray:
    """The exact no-overlap pass. In `order`, each disc moves outward along its ray
    from the origin to the first distance that is at least `floor` and clears every
    disc placed before it, so every pair ends at least pad_i + pad_j apart.

    Along the ray t*u, a placed disc c blocks the open interval of t where
    |t*u - c| < pad + pad_c: b -+ sqrt(need^2 - |c - b*u|^2) with b = u.c. The sweep
    takes the smallest t past the start that no interval covers.
    """
    unit = _unit(pos)
    start = np.maximum(np.hypot(pos[:, 0], pos[:, 1]), floor)
    out = np.empty_like(pos)
    placed = np.empty_like(pos)
    placed_pad = np.empty_like(pad)
    for m, i in enumerate(order):
        c = placed[:m]
        b = c @ unit[i]
        perp = c - b[:, None] * unit[i]
        need = placed_pad[:m] + pad[i]
        room = need * need - np.einsum("ij,ij->i", perp, perp)
        hit = room > 0.0
        half = np.sqrt(room[hit])
        lo, hi = b[hit] - half, b[hit] + half
        t = float(start[i])
        for k in np.argsort(lo, kind="stable"):
            if lo[k] >= t:
                break
            t = max(t, float(hi[k]))
        out[i] = placed[m] = unit[i] * t
        placed_pad[m] = pad[i]
    return out


def place_bubbles(cells: dict[str, dict], bubbled: list[str], synapses: list[dict],
                  pos_by_id: dict[str, tuple[float, float]]
                  ) -> dict[str, tuple[float, float, float]]:
    """Park the big outside libraries' lone decls in one disc per module (see the
    module docstring). `pos_by_id` holds the placed core on entry and gains every
    member. Returns {module: (x, y, radius)}.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    module_of: dict[str, str] = {}
    for cid in sorted(bubbled):
        module_of[cid] = bubble_module(cid, cells[cid].get("supercells"),
                                       len(cells[cid].get("organs") or ()))
        groups[module_of[cid]].append(cid)
    modules = sorted(groups)
    index = {m: i for i, m in enumerate(modules)}
    n = len(modules)

    # anchor: the synapse-weighted mean of the core cells a module's members bond with
    owner, xs, ys, ws = [], [], [], []
    for syn in synapses:
        for a, b in ((syn["src"], syn["dst"]), (syn["dst"], syn["src"])):
            mod, p = module_of.get(a), pos_by_id.get(b)
            if mod is not None and p is not None:
                w = syn.get("weight", 1)
                owner.append(index[mod])
                xs.append(w * p[0])
                ys.append(w * p[1])
                ws.append(w)
    wsum = np.bincount(owner, weights=ws, minlength=n) if owner else np.zeros(n)
    known = wsum > 0
    anchor = np.zeros((n, 2), dtype=np.float64)
    if owner:
        anchor[:, 0] = np.bincount(owner, weights=xs, minlength=n)
        anchor[:, 1] = np.bincount(owner, weights=ys, minlength=n)
        anchor[known] /= wsum[known, None]
    mean = anchor[known].mean(axis=0) if known.any() else np.zeros(2)
    for i, mod in enumerate(modules):
        if known[i]:
            continue
        anchor[i] = mean
        up = mod
        while "/" in up:                 # the nearest anchored ancestor module
            up = up.rsplit("/", 1)[0]
            j = index.get(up)
            if j is not None and known[j]:
                anchor[i] = anchor[j]
                break

    radius = SPAN / 2.0 * np.sqrt(np.array([len(groups[m]) for m in modules], dtype=np.float64))
    pad = radius * BUBBLE_GAP
    core = np.array([pos_by_id[c] for c in sorted(pos_by_id)], dtype=np.float64).reshape(-1, 2)
    core_radius = float(np.hypot(core[:, 0], core[:, 1]).max()) if len(core) else 0.0
    floor = core_radius + pad

    # Start just outside the core in the anchor's direction, offset by the disc's own
    # radius at a golden-angle turn: discs that share an anchor would otherwise start on
    # one ray, and every force in the relaxation keeps them on it.
    turn = np.arange(n, dtype=np.float64) * GOLDEN_ANGLE
    offset = np.stack([np.cos(turn), np.sin(turn)], axis=1) * radius[:, None]
    pos = _unit(anchor) * floor[:, None] + offset
    pos = _relax_discs(pos, anchor, pad, floor)
    order = sorted(range(n), key=lambda i: (-len(groups[modules[i]]), modules[i]))
    centre = _clear_discs(pos, pad, floor, order)

    discs: dict[str, tuple[float, float, float]] = {}
    for i, mod in enumerate(modules):
        spiral = _phyllotaxis(len(groups[mod]), float(radius[i])) + centre[i]
        for cid, p in zip(groups[mod], spiral):
            pos_by_id[cid] = (float(p[0]), float(p[1]))
        discs[mod] = (float(centre[i, 0]), float(centre[i, 1]), float(radius[i]))
    return discs


def layout_cells(cells: dict[str, dict], synapses: list[dict], *,
                 iterations: int = 200) -> dict[str, list[float]]:
    """Compute `xy` for every cell (mutates `cells`). Returns each module bubble's disc as
    {module: [x, y, radius]}, rounded to 0.1 like `xy`; {} when no cell is bubbled."""
    if np is None:
        raise ModuleNotFoundError("the layout needs numpy")
    ids = sorted(cells)
    bubbled = [c for c in ids if bubble_module(c, cells[c].get("supercells"),
                                               len(cells[c].get("organs") or ())) is not None]
    discs: dict[str, tuple[float, float, float]] = {}
    if not bubbled:
        rounded = _layout_core(cells, ids, synapses, iterations)
    else:
        members = set(bubbled)
        core = [c for c in ids if c not in members]
        core_syn = [s for s in synapses if s["src"] not in members and s["dst"] not in members]
        rounded = _layout_core(cells, core, core_syn, iterations)
        pos_by_id = {cid: (round(x, 1), round(y, 1)) for cid, (x, y) in rounded.items()}
        discs = place_bubbles(cells, bubbled, synapses, pos_by_id)
        for cid in bubbled:
            rounded[cid] = (round(pos_by_id[cid][0], 1), round(pos_by_id[cid][1], 1))
        _resolve_collisions(rounded, fixed=set(core))
    for cid in ids:
        cells[cid]["xy"] = [round(rounded[cid][0], 1), round(rounded[cid][1], 1)]
    return {mod: [round(x, 1), round(y, 1), round(r, 1)] for mod, (x, y, r) in sorted(discs.items())}


def _layout_core(cells: dict[str, dict], ids: list[str], synapses: list[dict],
                 iterations: int) -> dict[str, tuple[float, float]]:
    """Simulate the connected cells of `ids` and park the rest; returns the positions
    on the rounded grid that ships, collisions resolved."""
    if not ids:
        return {}
    if len(ids) == 1:
        return {ids[0]: (0.0, 0.0)}

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

    # Resolve on the ROUNDED coordinates that actually ship — rounding to 1dp can
    # itself collide two cells that were 0.05 apart, so resolving before rounding
    # would not guarantee the property.
    rounded = {cid: (round(pos_by_id[cid][0], 1), round(pos_by_id[cid][1], 1))
               for cid in ids}
    _resolve_collisions(rounded)
    return rounded
