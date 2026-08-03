#!/usr/bin/env python3
"""Figure F2 — repaired-grid five-arm retrieval results with clustered CIs
and published anchors.

Every plotted value is read from, and asserted against, the analysis JSONs:
  - bench/analysis/grid_repaired.json   (race-repaired arm points, "after")
  - bench/analysis/union_ablation.json  (declaration-clustered / task-bootstrap
                                         per-arm CIs on the same repaired rows)
Anchors: report §5 (0.775/0.780 QR R@10; 0.548/0.623 QR nDCG@10;
0.461/0.380/0.165 MPR), provenance in
docs/research/review/related_work_notes.md §5.
Run:  uv run --with matplotlib python3 fig_f2_retrieval.py
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

GRIDJ = json.loads((REPO / "bench/analysis/grid_repaired.json").read_text())
UA = json.loads((REPO / "bench/analysis/union_ablation.json").read_text())

BLUE = "#2a78d6"
ORANGE = "#eb6834"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#d9d8d4"

plt.rcParams.update({
    "font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 9,
    "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "legend.fontsize": 8.5,
    "axes.edgecolor": MUTED, "axes.linewidth": 0.8,
    "text.color": INK, "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "pdf.fonttype": 42,
})

ARMS = ["N", "F", "W", "U", "WF"]
after = GRIDJ["arm_tables"]["after"]

# ---- per-arm points + CIs, cross-asserted between the two JSONs -------------
qr_r, qr_n, mpr = {}, {}, {}
for a in ARMS:
    qr_r[a] = UA["qr810"]["cluster_bootstrap"]["recall@10"]["arms"][a]
    qr_n[a] = UA["qr810"]["cluster_bootstrap"]["ndcg@10"]["arms"][a]
    assert abs(qr_r[a]["point"] - after["qr810"][a]["recall@10"]) < 1e-9, a
    assert abs(qr_n[a]["point"] - after["qr810"][a]["ndcg@10"]) < 1e-9, a
    m = UA["mpr"]["arms"][a]
    mpr[a] = {"point": m["group_recall@10_per_task_mean"],
              "ci95": m["per_task_mean_boot_ci95"]}
    assert abs(mpr[a]["point"] - after["mpr"][a]["group_recall@10"]) < 1e-9, a

# headline assertions against the report's printed values
assert abs(qr_r["F"]["point"] - 0.8457) < 1e-9   # repaired F QR R@10 (0.846)
assert abs(qr_r["W"]["point"] - 0.8160) < 1e-9
assert abs(qr_r["WF"]["point"] - 0.8852) < 1e-9
assert abs(mpr["F"]["point"] - 0.5468) < 1e-9    # repaired F MPR (0.547)
assert abs(mpr["W"]["point"] - 0.2721) < 1e-9

ANCHORS = {  # (label, value, place-label-above?)
    "qr_r": [("TheoremGraph 0.775", 0.775, False), ("LSv2 0.780", 0.780, True)],
    "qr_n": [("TheoremGraph 0.548", 0.548, False), ("LSv2 0.623", 0.623, True)],
    "mpr": [("TG 0.165", 0.165, False), ("DIVER 0.380", 0.380, False),
            ("LSv2 reasoning 0.461", 0.461, True)],
}

# --------------------------------------------------------------------- plot --
fig, axes = plt.subplots(1, 3, figsize=(6.3, 2.9))
panels = [
    (axes[0], qr_r, "R@10", "MathlibQR fair-810", ANCHORS["qr_r"]),
    (axes[1], qr_n, "nDCG@10", "MathlibQR fair-810", ANCHORS["qr_n"]),
    (axes[2], mpr, "group-recall@10", "MathlibMPR", ANCHORS["mpr"]),
]
for ax, vals, ylabel, title, anchors in panels:
    xs = range(len(ARMS))
    heights = [vals[a]["point"] for a in ARMS]
    errs = [[vals[a]["point"] - vals[a]["ci95"][0] for a in ARMS],
            [vals[a]["ci95"][1] - vals[a]["point"] for a in ARMS]]
    colors = [BLUE] * len(ARMS)
    hatches = ["" if a != "WF" else "///" for a in ARMS]
    bars = ax.bar(xs, heights, 0.62, color=colors, zorder=3)
    for b, h in zip(bars, hatches):
        if h:
            b.set_hatch(h)
            b.set_edgecolor("white")
            b.set_linewidth(0)
    ax.errorbar(xs, heights, yerr=errs, fmt="none", ecolor=INK,
                elinewidth=0.9, capsize=2.0, zorder=4)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_ylim(0, 1.02)
    ax.yaxis.set_major_locator(MultipleLocator(0.2))
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", length=0)
    ax.set_xticks(list(xs), [a if a != "WF" else "WF†" for a in ARMS],
                  fontsize=8.5)
    ax.set_title(title, loc="left", fontsize=8.5)
    for name, yv, above in anchors:
        ax.axhline(yv, color=INK, lw=0.8, ls=(0, (4, 3)), zorder=4)
        ax.annotate(name, (-0.55, yv + (0.018 if above else -0.018)),
                    ha="left", va="bottom" if above else "top",
                    fontsize=6.4, color=INK, zorder=5,
                    bbox=dict(boxstyle="square,pad=0.12", facecolor="white",
                              edgecolor="none", alpha=0.75))
axes[2].annotate("† WF: post-hoc,\nbenchmark-informed", (0.03, 0.86),
                 xycoords="axes fraction", fontsize=6.6, color=MUTED,
                 style="italic", va="top")
fig.tight_layout(w_pad=1.6)
fig.savefig(FIG / "f2_retrieval_repaired.pdf", bbox_inches="tight")
plt.close(fig)
print("wrote", FIG / "f2_retrieval_repaired.pdf")
