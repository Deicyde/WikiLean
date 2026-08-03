#!/usr/bin/env python3
"""Figure F3 — retrieval provenance decomposition (report §5): for every
MathlibQR hit, where the gold name first entered the transcript.

Every plotted value is read from, and asserted against,
bench/analysis/retrieval_provenance.json (the resolved full-transcript
pass; an as-run trace — F's row includes its 175 zero-tool race rows,
whose hits count as memory, so F's memory share is an as-run upper bound).
Run:  uv run --with matplotlib python3 fig_f3_provenance.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "bench").is_dir())
FIG = HERE / "figures"

PROV = json.loads((REPO / "bench/analysis/retrieval_provenance.json").read_text())

BLUE = "#2a78d6"
ORANGE = "#eb6834"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#d9d8d4"
GRAY = "#b3b2ae"

plt.rcParams.update({
    "font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 9,
    "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "legend.fontsize": 8.5,
    "axes.edgecolor": MUTED, "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "pdf.fonttype": 42,
})

ARMS = ["W", "WF", "F"]
CATS = [("surfaced", "surfaced by a tool", ORANGE),
        ("guessed_verified", "guessed, then verified", BLUE),
        ("memory", "pure memory", GRAY),
        ("in_query", "in the query", "#87b4ea")]

# ---- extract + assert the report §5 table values ----------------------------
data = {}
for arm in ARMS:
    a = PROV["arms"][arm]
    res = a["resolved"]
    counts = {c: res.get(c, {}).get("n", 0) for c, _, _ in CATS}
    n_hits = a["n_hits"]
    assert sum(counts.values()) == n_hits, (arm, counts, n_hits)
    data[arm] = (counts, n_hits)

want = {  # the report §5 table: hits, surfaced, guessed_verified, memory
    "W": (661, 69, 582, 2),
    "WF": (717, 273, 424, 12),
    "F": (673, 151, 170, 344),
}
for arm, (hits, s, g, m) in want.items():
    counts, n = data[arm]
    assert n == hits and counts["surfaced"] == s \
        and counts["guessed_verified"] == g and counts["memory"] == m, arm
assert abs(PROV["arms"]["W"]["resolved"]["guessed_verified"]["frac"] - 0.8805) < 1e-9
assert abs(PROV["arms"]["W"]["resolved"]["surfaced"]["frac"] - 0.1044) < 1e-9

# --------------------------------------------------------------------- plot --
fig, ax = plt.subplots(figsize=(6.3, 2.5))
ys = [2, 1, 0]
for cat, label, color in CATS:
    lefts, vals = [], []
    for arm in ARMS:
        counts, n = data[arm]
        done = sum(counts[c] for c, _, _ in CATS[: [c for c, _, _ in CATS].index(cat)])
        lefts.append(100.0 * done / n)
        vals.append(100.0 * counts[cat] / n)
    ax.barh(ys, vals, left=lefts, height=0.58, color=color, zorder=3,
            edgecolor="white", linewidth=0.5, label=label)
    for y, left, v, arm in zip(ys, lefts, vals, ARMS):
        if v >= 7:
            ax.annotate(f"{v:.0f}%", (left + v / 2, y), ha="center",
                        va="center", fontsize=7.6,
                        color="white" if color in (BLUE, ORANGE) else INK)
ax.set_yticks(ys, [f"{arm}  ({data[arm][1]} hits)" for arm in ARMS])
ax.spines[["top", "right", "left"]].set_visible(False)
ax.tick_params(axis="y", length=0)
ax.set_xlim(0, 100)
ax.xaxis.set_major_locator(MultipleLocator(20))
ax.grid(axis="x", color=GRID, linewidth=0.6, zorder=0)
ax.set_axisbelow(True)
ax.set_xlabel("share of MathlibQR hits (%), by first entry of the gold name")
ax.annotate("F: as-run trace — its 175 zero-tool race rows count as memory",
            (0.995, 1.02), xycoords="axes fraction", ha="right", fontsize=7.2,
            color=MUTED, style="italic")
ax.legend(frameon=False, ncols=4, loc="upper center",
          bbox_to_anchor=(0.5, -0.30), columnspacing=1.1,
          handlelength=1.2, handletextpad=0.5)
fig.tight_layout()
fig.savefig(FIG / "f3_provenance.pdf", bbox_inches="tight")
plt.close(fig)
print("wrote", FIG / "f3_provenance.pdf")
