#!/usr/bin/env python3
"""Figure F4 — the v2 tool-use census (the report's fig5), refreshed on the
race-repaired run rows (the current bench/v2/runs tree; the 194 condemned
F/W originals live in bench/v2/runs/agent/race_condemned_archive/ and are
not scanned here).

Values are recomputed from transcript_stats.tool_calls_by_name over the run
rows and asserted against the repaired-rows census stated in the supplement
(§S6): W 3.5 (QR) / 10.6 (MPR) calls/run, repaired F 2.8 / 8.2, WF 4.6
pooled, WF brain_cell exactly 1, and SorryDB-WF formal share 94%.
Output keeps the name the supplement references: figures/fig5_tooluse.pdf.
Run:  uv run --with matplotlib python3 fig_f4_tooluse.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "bench").is_dir())
FIG = HERE / "figures"
V2RUNS = REPO / "bench/v2/runs"

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

WIKI = ["decl_exists", "brain_bridge", "brain_search", "brain_cell",
        "brain_transfer", "brain_neighborhood", "brain_snippets", "brain_filter"]
FORMAL = ["loogle", "decl_grep", "decl_read"]


def tool_sums(paths):
    """Sum tool_calls_by_name over run rows -> (canonical counts, n_runs)."""
    canon, n = defaultdict(int), 0
    for f in paths:
        row = json.loads(f.read_text())
        n += 1
        calls = (row.get("transcript_stats") or {}).get("tool_calls_by_name") or {}
        for name, k in calls.items():
            base = name.split("__")[-1]
            canon[base if base in WIKI + FORMAL else "other"] += k
    return dict(canon), n


per = {}
for bench in ("qr810", "mpr"):
    for arm in ("W", "F", "WF"):
        per[(bench, arm)] = tool_sums(
            sorted((V2RUNS / "agent" / bench / arm / "claude-sonnet-5").glob("*.json")))
for arm in ("F", "WF"):
    per[("sorrydb", arm)] = tool_sums(
        sorted((V2RUNS / "sorrydb" / arm / "claude-sonnet-5").glob("*.json")))


def pooled(arm):
    c, n = defaultdict(int), 0
    for bench in ("qr810", "mpr"):
        cc, m = per[(bench, arm)]
        n += m
        for k, v in cc.items():
            c[k] += v
    return dict(c), n


retrieval = {arm: pooled(arm) for arm in ("W", "F", "WF")}

# ---- assertions against the supplement §S6 repaired-rows census -------------
def calls_per_run(bench, arm):
    c, n = per[(bench, arm)]
    return sum(c.values()) / n

assert round(calls_per_run("qr810", "W"), 1) == 3.5
assert round(calls_per_run("mpr", "W"), 1) == 10.6
assert round(calls_per_run("qr810", "F"), 1) == 2.8   # repaired (as-run 2.2)
assert round(calls_per_run("mpr", "F"), 1) == 8.2
wf_c, wf_n = retrieval["WF"]
assert round(sum(wf_c.values()) / wf_n, 1) == 4.6
assert wf_c["brain_cell"] == 1                        # manual kills the misuse
sdb_wf, _ = per[("sorrydb", "WF")]
formal_share = 100.0 * sum(sdb_wf.get(t, 0) for t in FORMAL) / sum(sdb_wf.values())
assert round(formal_share) == 94
print("census assertions passed",
      {b + "/" + a: round(calls_per_run(b, a), 1)
       for b in ("qr810", "mpr") for a in ("W", "F", "WF")})

# --------------------------------------------------------------------- plot --
wiki_c = ["#123c74", "#2a78d6", "#5b96e0", "#87b4ea",
          "#a9c9f1", "#c3d9f6", "#d9e7fa", "#ecf4fd"]
formal_c = ["#a63d10", "#eb6834", "#f5a878"]
other_c = "#b3b2ae"
rows = [("retrieval", "W"), ("retrieval", "F"), ("retrieval", "WF"),
        ("sorrydb", "F"), ("sorrydb", "WF")]
T = {("retrieval", a): retrieval[a] for a in ("W", "F", "WF")}
T[("sorrydb", "F")] = per[("sorrydb", "F")]
T[("sorrydb", "WF")] = per[("sorrydb", "WF")]

ys = [4.55, 3.55, 2.55, 1.0, 0.0]
fig, ax = plt.subplots(figsize=(6.3, 3.5))
order = WIKI + FORMAL + ["other"]
for tool, color in (list(zip(WIKI, wiki_c)) + list(zip(FORMAL, formal_c))
                    + [("other", other_c)]):
    lefts = [sum(T[key][0].get(t, 0) for t in order[: order.index(tool)])
             for key in rows]
    vals = [T[key][0].get(tool, 0) for key in rows]
    label = tool if tool != "other" else "other (built-ins)"
    ax.barh(ys, vals, left=lefts, height=0.62, color=color, zorder=3,
            edgecolor="white", linewidth=0.4, label=label)
totals = [sum(T[key][0].values()) for key in rows]
xmax = max(totals) * 1.12
for y, tot in zip(ys, totals):
    ax.annotate(f"{tot:,}", (tot + xmax * 0.008, y), va="center", fontsize=8,
                color=INK)
ax.set_yticks(ys, [f"{arm} ({T[(fam, arm)][1]} runs)" for fam, arm in rows])
ax.spines[["top", "right", "left"]].set_visible(False)
ax.tick_params(axis="y", length=0)
ax.set_xlim(0, xmax)
ax.grid(axis="x", color=GRID, linewidth=0.6, zorder=0)
ax.set_axisbelow(True)
ax.set_xlabel("total tool calls (summed over run rows; race-repaired grid)")
ax.annotate("retrieval: MathlibQR-810 + MPR", (0, 5.28), fontsize=8.5,
            color=MUTED, style="italic", annotation_clip=False)
ax.annotate("SorryDB", (0, 1.73), fontsize=8.5, color=MUTED,
            style="italic", annotation_clip=False)
ax.legend(frameon=False, ncols=3, loc="upper center",
          bbox_to_anchor=(0.5, -0.28), columnspacing=1.1,
          handlelength=1.2, handletextpad=0.5)
fig.tight_layout()
fig.savefig(FIG / "fig5_tooluse.pdf", bbox_inches="tight")
plt.close(fig)
print("wrote", FIG / "fig5_tooluse.pdf")
