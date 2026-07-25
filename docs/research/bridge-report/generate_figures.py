#!/usr/bin/env python3
"""Generate the four report figures (vector PDF) from figures/figdata.json.

figdata.json is produced by recompute_figdata.py, which recomputes every value
from the raw benchmark data and asserts it against BRIDGE-REPORT.md.
Run:  uv run --with matplotlib python3 generate_figures.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
D = json.loads((FIG / "figdata.json").read_text())

# Colorblind-safe pair (validated: CVD dE 24.7 protan / 32.7 tritan, normal 33.6)
BLUE = "#2a78d6"   # eval / primary series
ORANGE = "#eb6834"  # fresh / secondary series
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

ARMS = ["A", "B", "C", "D", "E"]
ARM_LABELS = ["A\nno tools", "B\ninformal", "C\nformal", "D\nwikibrain", "E\nB+C unjoined"]


def style(ax, ymax, ystep, ylabel):
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_ylim(0, ymax)
    ax.yaxis.set_major_locator(MultipleLocator(ystep))
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", length=0)


def bar_labels(ax, bars, fmt, dy=0.5, fs=8):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()),
                    (b.get_x() + b.get_width() / 2, b.get_height() + dy),
                    ha="center", va="bottom", fontsize=fs, color=INK)


# ---------------------------------------------------------------- Figure 1 --
def fig1():
    ev = [D["tier1"]["success"]["eval"][a] for a in ARMS]
    fr = [D["tier1"]["success"]["fresh"][a] for a in ARMS]
    fig, ax = plt.subplots(figsize=(6.3, 2.9))
    x = range(len(ARMS))
    w = 0.38
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], ev, w, color=BLUE, zorder=3,
                label="ProofNet# eval (n=371, contaminated)")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], fr, w, color=ORANGE, zorder=3,
                label="fresh set (n=100, contamination-proof)")
    style(ax, 78, 20, "success (%)")
    ax.set_xticks(list(x), ARM_LABELS)
    bar_labels(ax, b1, "{:.1f}")
    bar_labels(ax, b2, "{:.0f}")
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0.0, 1.02))
    # the headline movement: A's off-distribution collapse
    ax.annotate("memorization\ncollapse", xy=(0.24, 12), xytext=(0.95, 33),
                fontsize=8, color=MUTED, ha="center",
                arrowprops=dict(arrowstyle="->", color=MUTED, lw=0.8))
    fig.tight_layout()
    fig.savefig(FIG / "fig1_tier1_success.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- Figure 2 --
def fig2():
    ev = [D["tier1"]["halluc"]["eval"][a] for a in ARMS]
    fr = [D["tier1"]["halluc"]["fresh"][a] for a in ARMS]
    fig, ax = plt.subplots(figsize=(6.3, 2.9))
    x = range(len(ARMS))
    w = 0.38
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], ev, w, color=BLUE, zorder=3,
                label="ProofNet# eval")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], fr, w, color=ORANGE, zorder=3,
                label="fresh set")
    style(ax, 30, 10, "hallucinated-decl rate (%)")
    ax.set_xticks(list(x), ARM_LABELS)
    bar_labels(ax, b1, "{:.1f}", dy=0.3)
    bar_labels(ax, b2, "{:.1f}", dy=0.3)
    ax.legend(frameon=False, loc="upper left")
    ax.annotate("lower is better", xy=(0.50, 0.97), xycoords="axes fraction",
                ha="center", va="top", fontsize=8, color=MUTED, style="italic")
    fig.tight_layout()
    fig.savefig(FIG / "fig2_halluc.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- Figure 3 --
def fig3():
    order = ["system", "N", "W", "F", "WF"]
    labels = ["system\nmode", "N", "W", "F", "WF"]
    qr = [D["retrieval"]["qr_r10"][s] for s in order]
    mpr = [D["retrieval"]["mpr_gr10"][s] for s in order]
    A = D["retrieval"]["anchors"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.3, 2.9))
    for ax, vals, ylabel, anchors in (
            (ax1, qr, "R@10",
             [("LeanSearch-v2 0.780", A["qr_lsv2"], 0.018),
              ("TheoremGraph 0.775", A["qr_theoremgraph"], -0.030)]),
            (ax2, mpr, "group-recall@10",
             [("LSv2 reasoning 0.461", A["mpr_lsv2"], 0.018),
              ("DIVER 0.380", A["mpr_diver"], -0.030)])):
        bars = ax.bar(range(len(order)), vals, 0.62, color=BLUE, zorder=3)
        style(ax, 1.02, 0.2, ylabel)
        ax.set_xticks(range(len(order)), labels, fontsize=8)
        bar_labels(ax, bars, "{:.3f}", dy=0.012, fs=7.5)
        for name, y, dy in anchors:
            ax.axhline(y, color=INK, lw=0.9, ls=(0, (4, 3)), zorder=4)
            ax.annotate(name, (-0.55, y + dy), ha="left",
                        va="bottom" if dy > 0 else "top", fontsize=7, color=INK)
    ax1.set_title("MathlibQR fair-810 (concept retrieval)", loc="left")
    ax2.set_title("MathlibMPR (premise retrieval)", loc="left")
    fig.tight_layout(w_pad=2.0)
    fig.savefig(FIG / "fig3_retrieval.pdf")
    plt.close(fig)


# ---------------------------------------------------------------- Figure 4 --
def fig4():
    arms = ["N", "F", "WF"]
    labels = ["N\nno tools", "F\nformal", "WF\nunion+manual"]
    proved = [D["sorrydb"][a]["proved"] for a in arms]
    cpp = [D["sorrydb"][a]["cost_per_proved"] for a in arms]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.3, 2.7))
    b1 = ax1.bar(range(3), proved, 0.55, color=BLUE, zorder=3)
    style(ax1, 12, 2, "sorries proved (kernel), of 171")
    ax1.set_xticks(range(3), labels)
    bar_labels(ax1, b1, "{:.0f}", dy=0.15)
    b2 = ax2.bar(range(3), cpp, 0.55, color=ORANGE, zorder=3)
    style(ax2, 33, 10, "cost per proved theorem (USD)")
    ax2.set_xticks(range(3), labels)
    bar_labels(ax2, b2, "${:.2f}", dy=0.4)
    ax2.annotate("lower is better", xy=(0.99, 0.97), xycoords="axes fraction",
                 ha="right", va="top", fontsize=8, color=MUTED, style="italic")
    ax1.set_title("kernel-verified proofs", loc="left")
    ax2.set_title("cost per proved theorem", loc="left")
    fig.tight_layout(w_pad=2.0)
    fig.savefig(FIG / "fig4_sorrydb.pdf")
    plt.close(fig)


for f in (fig1, fig2, fig3, fig4):
    f()
print("wrote", *sorted(p.name for p in FIG.glob("fig*.pdf")))
