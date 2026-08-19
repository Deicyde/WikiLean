#!/usr/bin/env python3
"""Figure F1 — forest plot of the commit-clustered paired risk differences
(fresh-100, repaired instrument) for the headline Tier-1 contrasts.

Every plotted value is read from, and asserted against, the analysis JSONs:
  - bench/analysis/success_repaired.json  (grounded typecheck: D-A, D-C, D-E)
  - bench/analysis/v3_gate_fixes.json     (grounded typecheck E-A; halluc D-A,
                                           E-A; conjunction D-A, E-A, D-E)
Run:  uv run --with matplotlib python3 fig_f1_forest.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "bench").is_dir())
FIG = HERE / "figures"

SR = json.loads((REPO / "bench/analysis/success_repaired.json").read_text())
GF = json.loads((REPO / "bench/analysis/v3_gate_fixes.json").read_text())

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


def boot(d):
    """Extract (rd, lo, hi, p) from a commit-clustered bootstrap block."""
    ci = d["ci95_percentile"]
    return d["rd"], ci[0], ci[1], d["p_two_sided_percentile_inversion"]


# ---- assemble the nine contrasts, asserting the expected headline values ----
gtc = SR["commit_clustered_bootstrap"]["repaired_oracle"]
rows = []  # (group, label, rd, lo, hi, p)

for pair, want_rd in [("D_vs_A", 0.27), ("D_vs_C", 0.15), ("D_vs_E", 0.11)]:
    rd, lo, hi, p = boot(gtc[pair])
    assert abs(rd - want_rd) < 1e-9, (pair, rd, want_rd)
    rows.append(("Grounded typecheck", pair, rd, lo, hi, p))
rd, lo, hi, p = boot(GF["2_commit_clustered_bootstraps"]["gtc_repaired.E_vs_A"]
                     ["commit_clustered_bootstrap"])
assert abs(rd - 0.16) < 1e-9
rows.insert(1, ("Grounded typecheck", "E_vs_A", rd, lo, hi, p))

for key, want_rd in [("halluc_repaired.D_vs_A", -0.31),
                     ("halluc_repaired.E_vs_A", -0.27)]:
    rd, lo, hi, p = boot(GF["2_commit_clustered_bootstraps"][key]
                         ["commit_clustered_bootstrap"])
    assert abs(rd - want_rd) < 1e-9, (key, rd, want_rd)
    rows.append(("Runs with a flagged citation", key.split(".")[1], rd, lo, hi, p))

for key, want_rd in [("conj_repaired.D_vs_A", 0.08),
                     ("conj_repaired.E_vs_A", 0.13),
                     ("conj_repaired.D_vs_E", -0.05)]:
    rd, lo, hi, p = boot(GF["2_commit_clustered_bootstraps"][key]
                         ["commit_clustered_bootstrap"])
    assert abs(rd - want_rd) < 1e-9, (key, rd, want_rd)
    rows.append(("Typecheck AND judge-evaluated", key.split(".")[1], rd, lo, hi, p))

# spot-check two intervals against the report's printed values
assert abs(gtc["D_vs_A"]["ci95_percentile"][0] - 0.130952) < 1e-6
assert abs(gtc["D_vs_A"]["ci95_percentile"][1] - 0.394737) < 1e-6
assert abs(gtc["D_vs_A"]["p_two_sided_percentile_inversion"] - 0.0004) < 1e-9

# pair code + a short gloss (A = no tools; C = formal search; D = the Brain,
# join + verifier; E = both corpora, unjoined)
PAIR_LABEL = {"D_vs_A": ("D − A", "Brain vs no tools"),
              "E_vs_A": ("E − A", "unjoined vs no tools"),
              "D_vs_C": ("D − C", "Brain vs formal"),
              "D_vs_E": ("D − E", "Brain vs unjoined")}
# label columns and the stats column live in figure-fraction x; the axes box
# is pinned (no tight_layout) so the geometry cannot collapse.
HDR_X, LBL_X, GLOSS_X, STAT_X = 0.015, 0.045, 0.115, 0.685


def fmt_p(p):
    return f"p={p:.4f}".rstrip("0").rstrip(".") if p >= 0.001 else f"p={p:.1g}"


# --------------------------------------------------------------------- plot --
fig = plt.figure(figsize=(9.4, 3.6))
ax = fig.add_axes([0.27, 0.15, 0.40, 0.77])
groups = ["Grounded typecheck", "Runs with a flagged citation",
          "Typecheck AND judge-evaluated"]
y = 0.0
ys, headers = [], []
for g in groups:
    headers.append((y, g))
    y -= 0.75
    for (grp, pair, rd, lo, hi, p) in rows:
        if grp != g:
            continue
        sig = (lo > 0) or (hi < 0)
        color = BLUE if sig else MUTED
        ax.plot([lo, hi], [y, y], color=color, lw=1.4, zorder=3,
                solid_capstyle="butt")
        for end in (lo, hi):
            ax.plot([end, end], [y - 0.10, y + 0.10], color=color, lw=1.2,
                    zorder=3)
        ax.plot([rd], [y], marker="s", ms=5.5, color=color, zorder=4)
        code, gloss = PAIR_LABEL[pair]
        ax.annotate(code, (LBL_X, y), xycoords=("figure fraction", "data"),
                    va="center", ha="left",
                    fontsize=8.5, color=INK, annotation_clip=False)
        ax.annotate(gloss, (GLOSS_X, y), xycoords=("figure fraction", "data"),
                    va="center", ha="left",
                    fontsize=7.3, color=MUTED, annotation_clip=False)
        ax.annotate(f"{rd:+.2f}  [{lo:+.2f}, {hi:+.2f}]   {fmt_p(p)}",
                    (STAT_X, y), xycoords=("figure fraction", "data"),
                    va="center", ha="left", fontsize=7.8,
                    color=INK, annotation_clip=False)
        ys.append(y)
        y -= 0.55
    y -= 0.30

for hy, g in headers:
    ax.annotate(g, (HDR_X, hy), xycoords=("figure fraction", "data"),
                va="center", ha="left", fontsize=9,
                color=INK, fontweight="bold", annotation_clip=False)

ax.axvline(0.0, color=INK, lw=0.9, zorder=2)
ax.set_xlim(-0.50, 0.52)
ax.set_ylim(min(ys) - 0.55, 0.45)
ax.set_yticks([])
ax.spines[["top", "right", "left"]].set_visible(False)
ax.grid(axis="x", color=GRID, linewidth=0.6, zorder=0)
ax.set_axisbelow(True)
ax.set_xlabel("paired risk difference (commit-clustered bootstrap, 44 clusters, "
              "B=10,000; whiskers: 95% confidence, exact asymmetric bounds)")
ax.annotate("favors the second-named arm  ←", (-0.02, 0.42), ha="right",
            fontsize=7.8, color=MUTED, style="italic", annotation_clip=False)
ax.annotate("→  favors the first-named arm", (0.02, 0.42), ha="left",
            fontsize=7.8, color=MUTED, style="italic", annotation_clip=False)
fig.savefig(FIG / "f1_forest.pdf", bbox_inches="tight")
plt.close(fig)
print("wrote", FIG / "f1_forest.pdf", f"({len(ys)} contrasts)")
