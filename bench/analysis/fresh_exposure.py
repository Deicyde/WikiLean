#!/usr/bin/env python3
"""Fresh-set exposure + robustness analysis for the Bridge Experiment report v2.

Questions answered (all against the PINNED tree = mathlib4 commit 61a5e4f338,
the checkout state during the Jul 18-19 fresh runs, content date 2026-07-10):

  (1) EXPOSURE TABLE - for each of the 100 fresh tasks, was the gold declaration
      (a) present anywhere in the tree as a decl-keyword header (basename),
      (b) present as a decl-keyword header in the task's OWN module file,
      plus: verbatim full dotted name anywhere, module-file existence, and
      whether the gold's `added_in.commit` is a git ancestor of the pin.
  (2) ROBUSTNESS SPLIT - per-arm grounded-typecheck rate (success in
      bridge_summary paired_matrix = produced AND no-halluc AND typecheck)
      separately on exposed (own-module basis) vs unexposed tasks, with Wilson
      95% CIs, and D-vs-E / D-vs-C discordant pairs + exact McNemar p within
      each stratum.
  (3) Same per-arm rates split by whether the gold was merged before/after
      2026-07-10 (primary definition: added_in.commit is an ancestor of the
      pin; on this task set this coincides exactly with date < 2026-07-10).

IMPORTANT: outcomes are read from the SNAPSHOT copy of bridge_summary.json in
snapshot_fresh_orig/ (taken before a concurrent arm-E repair job could rewrite
rows fresh_069-099), and E attempted/errored status is read from the snapshot
run rows. The pinned tree is obtained by read-only `git archive` extraction -
the live checkout is never touched.

Reproduce:  python3 bench/analysis/fresh_exposure.py
Artifacts:  bench/analysis/fresh_exposure.json, bench/analysis/fresh_exposure.md
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/jack/Desktop/LEAN/WikiLean")
MATHLIB = Path("/Users/jack/Desktop/LEAN/mathlib4")
PIN = "61a5e4f338bfdddf2f6296402a49fe80f3b1a147"
PIN_SHORT = PIN[:10]
TASKS_FILE = REPO / "bench/data/fresh_tasks.jsonl"
ANALYSIS = REPO / "bench/analysis"
SNAPSHOT = ANALYSIS / "snapshot_fresh_orig"
SUMMARY_SNAPSHOT = SNAPSHOT / "bridge_summary.snapshot.json"
OUT_JSON = ANALYSIS / "fresh_exposure.json"
OUT_MD = ANALYSIS / "fresh_exposure.md"

# Known-good pre-extracted archive from the earlier verifier session (reused if
# present and file-complete); otherwise we extract fresh into a temp cache dir.
KNOWN_EXTRACTION = Path(
    "/private/tmp/claude-501/-Users-jack-Desktop-LEAN-WikiLean/"
    "0b16d2c8-53d8-49d0-8e6e-c03de5fb2eff/scratchpad/ml61"
)

ARMS = ["A", "B", "C", "D", "E"]
DECL_KW = r"(?:theorem|lemma|def|abbrev|instance|structure|class|inductive)"
IDENT = r"[A-Za-z0-9_'!?₀-ₜᵢ-ᵪʰ-˿ⁱⁿ]"


# ---------------------------------------------------------------- pinned tree
def tree_root() -> Path:
    """Return a directory containing Mathlib/ as of PIN (read-only extraction)."""
    expected = int(
        subprocess.run(
            ["git", "-C", str(MATHLIB), "ls-tree", "-r", PIN, "--name-only", "Mathlib"],
            capture_output=True, text=True, check=True,
        ).stdout.count(".lean")
    )
    candidates = [KNOWN_EXTRACTION,
                  Path(tempfile.gettempdir()) / f"wikilean_ml_{PIN_SHORT}"]
    for cand in candidates:
        if (cand / "Mathlib").is_dir():
            n = sum(1 for _ in (cand / "Mathlib").rglob("*.lean"))
            if n == expected:
                return cand
    dest = candidates[-1]
    dest.mkdir(parents=True, exist_ok=True)
    print(f"extracting {PIN_SHORT}:Mathlib -> {dest} ...", file=sys.stderr)
    ar = subprocess.Popen(
        ["git", "-C", str(MATHLIB), "archive", PIN, "Mathlib"],
        stdout=subprocess.PIPE,
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], stdin=ar.stdout, check=True)
    ar.wait()
    if ar.returncode:
        sys.exit("git archive failed")
    return dest


def is_ancestor(commit: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(MATHLIB), "merge-base", "--is-ancestor", commit, PIN],
        capture_output=True,
    )
    return r.returncode == 0


# ------------------------------------------------------------------ exposure
def compute_exposure(tasks, root: Path):
    names = [t["decl_name"] for t in tasks]
    basenames = [n.split(".")[-1] for n in names]
    full_rx = re.compile("|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True)))
    base_rx = re.compile(
        DECL_KW + r"\s+(?:" + IDENT + r"+\.)*(?P<name>"
        + "|".join(re.escape(b) for b in sorted(set(basenames), key=len, reverse=True))
        + r")(?!" + IDENT + r")"
    )
    full_hits: dict[str, list] = {n: [] for n in names}
    base_hits: dict[str, list] = {b: [] for b in basenames}
    for path in sorted((root / "Mathlib").rglob("*.lean")):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(root))
        for m in full_rx.finditer(text):
            g = m.group(0)
            if g in full_hits and len(full_hits[g]) < 5:
                full_hits[g].append(rel)
        for m in base_rx.finditer(text):
            b = m.group("name")
            if len(base_hits[b]) < 400:
                base_hits[b].append(rel)

    rows = []
    for t, n, b in zip(tasks, names, basenames):
        mod_rel = t["module"].replace(".", "/") + ".lean"
        mod_path = root / mod_rel
        module_exists = mod_path.exists()
        in_module = mod_rel in base_hits[b]
        if module_exists and not in_module:
            # direct re-scan guards against the per-basename hit cap
            one = re.compile(DECL_KW + r"\s+(?:" + IDENT + r"+\.)*"
                             + re.escape(b) + r"(?!" + IDENT + r")")
            in_module = bool(one.search(mod_path.read_text(encoding="utf-8", errors="replace")))
        rows.append({
            "id": t["id"],
            "decl_name": n,
            "module": t["module"],
            "added_date": t["added_in"]["date"],
            "added_commit": t["added_in"]["commit"],
            "gold_commit_in_pin": is_ancestor(t["added_in"]["commit"]),
            "full_name_anywhere": bool(full_hits[n]),
            "basename_decl_anywhere": bool(base_hits[b]),
            "basename_in_own_module": in_module,
            "module_file_exists_at_pin": module_exists,
            "full_name_hit_files": full_hits[n][:3],
        })
    return rows


# ---------------------------------------------------------------- statistics
def wilson(k: int, n: int, z: float = 1.959963984540054):
    if n == 0:
        return None, None, None
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial McNemar p-value on discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    tail = sum(math.comb(n, i) for i in range(lo + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def stratum_stats(ids, pm, e_attempted):
    out = {"n": len(ids), "task_ids": sorted(ids), "arms": {}, "pairs": {}}
    for arm in ARMS:
        k = sum(1 for i in ids if pm[i][arm])
        p, lo, hi = wilson(k, len(ids))
        out["arms"][arm] = {"k": k, "n": len(ids), "rate": round(p, 4),
                            "wilson95": [round(lo, 4), round(hi, 4)]}
    att = [i for i in ids if i in e_attempted]
    k = sum(1 for i in att if pm[i]["E"])
    p, lo, hi = wilson(k, len(att))
    out["arms"]["E_attempted_only"] = {
        "k": k, "n": len(att), "rate": round(p, 4) if p is not None else None,
        "wilson95": [round(lo, 4), round(hi, 4)] if p is not None else None,
        "note": "E restricted to snapshot rows without a 429 error (secondary; "
                "primary E numbers count errored rows as failures)"}
    for other in ["E", "C"]:
        b = sum(1 for i in ids if pm[i]["D"] and not pm[i][other])
        c = sum(1 for i in ids if pm[i][other] and not pm[i]["D"])
        both = sum(1 for i in ids if pm[i]["D"] and pm[i][other])
        out["pairs"][f"D_vs_{other}"] = {
            "both": both, "D_only": b, f"{other}_only": c,
            "neither": len(ids) - both - b - c,
            "discordant": b + c,
            "mcnemar_exact_p": float(f"{mcnemar_exact(b, c):.3g}")}
    return out


# ---------------------------------------------------------------------- main
def main():
    tasks = [json.loads(l) for l in open(TASKS_FILE)]
    assert len(tasks) == 100, "expected 100 fresh tasks"

    if not SUMMARY_SNAPSHOT.exists():
        sys.exit(f"snapshot summary missing: {SUMMARY_SNAPSHOT} - refusing to "
                 "read the live bridge_summary.json (concurrent E repair job)")
    summary = json.load(open(SUMMARY_SNAPSHOT))
    pm = {k: v for k, v in summary["paired_matrix"].items() if k.startswith("fresh_")}
    assert len(pm) == 100, "expected 100 fresh rows in paired_matrix"

    # E attempted status from the SNAPSHOT run rows
    e_attempted = set()
    for f in sorted((SNAPSHOT / "E").glob("fresh_*.json")):
        row = json.load(open(f))
        if "error" not in row:
            e_attempted.add(row["task_id"])

    root = tree_root()
    rows = compute_exposure(tasks, root)
    by_id = {r["id"]: r for r in rows}

    exposed = [r["id"] for r in rows if r["basename_in_own_module"]]
    unexposed = [r["id"] for r in rows if not r["basename_in_own_module"]]
    in_pin = [r["id"] for r in rows if r["gold_commit_in_pin"]]
    post_pin = [r["id"] for r in rows if not r["gold_commit_in_pin"]]
    before_date = [r["id"] for r in rows if r["added_date"] < "2026-07-10"]

    result = {
        "provenance": {
            "pinned_tree_commit": PIN,
            "pinned_tree_date": "2026-07-10",
            "tree_root": str(root),
            "tasks_file": str(TASKS_FILE),
            "outcomes_file": str(SUMMARY_SNAPSHOT),
            "success_metric": summary["success_metric"],
            "e_errored_rows_counted_as_failure": sorted(
                set(pm) - e_attempted),
            "date_vs_ancestor_note": (
                "date<2026-07-10 coincides exactly with ancestor-of-pin on this "
                "task set; the 7 tasks dated 2026-07-10 all landed after the pin "
                "commit and are in the post-pin stratum"),
        },
        "exposure_counts": {
            "full_name_anywhere": sum(r["full_name_anywhere"] for r in rows),
            "basename_decl_anywhere": sum(r["basename_decl_anywhere"] for r in rows),
            "basename_in_own_module": len(exposed),
            "gold_commit_in_pin": len(in_pin),
            "added_date_before_2026-07-10": len(before_date),
        },
        "per_task": rows,
        "robustness_split_by_exposure": {
            "definition": "exposed = gold basename appears as a decl-keyword "
                          "header in the task's own module file at the pin",
            "exposed": stratum_stats(exposed, pm, e_attempted),
            "unexposed": stratum_stats(unexposed, pm, e_attempted),
        },
        "split_by_merge_date": {
            "definition": "in_pin = added_in.commit is a git ancestor of the "
                          "pinned commit (== added_date < 2026-07-10 here)",
            "merged_before_pin": stratum_stats(in_pin, pm, e_attempted),
            "merged_after_pin": stratum_stats(post_pin, pm, e_attempted),
        },
        "overall_fresh": stratum_stats(list(pm), pm, e_attempted),
    }
    # keep task_ids only where short
    result["overall_fresh"].pop("task_ids", None)

    OUT_JSON.write_text(json.dumps(result, indent=2) + "\n")
    OUT_MD.write_text(render_md(result, by_id))
    print(f"wrote {OUT_JSON}\nwrote {OUT_MD}")
    hs = {
        "exposed_n": len(exposed), "unexposed_n": len(unexposed),
        "D_rate_exposed": result["robustness_split_by_exposure"]["exposed"]["arms"]["D"]["rate"],
        "D_rate_unexposed": result["robustness_split_by_exposure"]["unexposed"]["arms"]["D"]["rate"],
        "E_rate_exposed": result["robustness_split_by_exposure"]["exposed"]["arms"]["E"]["rate"],
        "E_rate_unexposed": result["robustness_split_by_exposure"]["unexposed"]["arms"]["E"]["rate"],
    }
    print(json.dumps(hs, indent=2))


def fmt_arm(a):
    if a["rate"] is None:
        return "-"
    return f"{a['k']}/{a['n']} = {a['rate']:.1%} [{a['wilson95'][0]:.1%}, {a['wilson95'][1]:.1%}]"


def render_md(result, by_id):
    L = []
    L.append("# Fresh-set exposure & robustness (Bridge report v2)\n")
    p = result["provenance"]
    L.append(f"Pinned tree: mathlib4 `{p['pinned_tree_commit'][:10]}` "
             f"(content date {p['pinned_tree_date']}), read-only `git archive` extraction. "
             f"Outcomes: snapshot of `bridge_summary.json` paired matrix "
             f"(success = {p['success_metric']}). "
             f"Arm E rows fresh_069-099 errored (session-limit 429) in the snapshot and count "
             f"as failures in primary E numbers; `E_attempted_only` rows exclude them.\n")
    ec = result["exposure_counts"]
    L.append("## Exposure counts (of 100 fresh tasks)\n")
    L.append("| flag | n |\n|---|---|")
    L.append(f"| gold full dotted name appears verbatim anywhere | {ec['full_name_anywhere']} |")
    L.append(f"| gold basename as decl-keyword header anywhere | {ec['basename_decl_anywhere']} |")
    L.append(f"| gold basename as decl header in task's own module | {ec['basename_in_own_module']} |")
    L.append(f"| gold commit is ancestor of pin (decl truly in tree) | {ec['gold_commit_in_pin']} |")
    L.append(f"| added_date strictly before 2026-07-10 | {ec['added_date_before_2026-07-10']} |\n")
    L.append(p["date_vs_ancestor_note"] + "\n")

    def stratum_block(title, s):
        L.append(f"### {title} (n = {s['n']})\n")
        L.append("| arm | grounded-typecheck | Wilson 95% CI |\n|---|---|---|")
        for arm in ARMS + ["E_attempted_only"]:
            a = s["arms"][arm]
            if a["rate"] is None:
                L.append(f"| {arm} | - | - |")
            else:
                L.append(f"| {arm} | {a['k']}/{a['n']} = {a['rate']:.1%} "
                         f"| [{a['wilson95'][0]:.1%}, {a['wilson95'][1]:.1%}] |")
        L.append("")
        L.append("| pair | both | D-only | other-only | neither | exact McNemar p |\n|---|---|---|---|---|---|")
        for pair, d in s["pairs"].items():
            other = pair.split("_vs_")[1]
            L.append(f"| {pair} | {d['both']} | {d['D_only']} | {d[f'{other}_only']} "
                     f"| {d['neither']} | {d['mcnemar_exact_p']} |")
        L.append("")

    L.append("## Robustness split: exposed vs unexposed (own-module basis)\n")
    L.append(result["robustness_split_by_exposure"]["definition"] + "\n")
    stratum_block("Exposed", result["robustness_split_by_exposure"]["exposed"])
    stratum_block("Unexposed", result["robustness_split_by_exposure"]["unexposed"])

    L.append("## Split by merge date vs the pin (2026-07-10)\n")
    L.append(result["split_by_merge_date"]["definition"] + "\n")
    stratum_block("Merged before pin (gold in tree)", result["split_by_merge_date"]["merged_before_pin"])
    stratum_block("Merged after pin (gold NOT in tree)", result["split_by_merge_date"]["merged_after_pin"])

    L.append("## Overall fresh set (n = 100)\n")
    stratum_block("All fresh", result["overall_fresh"])

    L.append("## Per-task exposure table\n")
    L.append("| id | gold decl | module | added | in-pin | full-name | base-anywhere | base-in-module |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in result["per_task"]:
        L.append(f"| {r['id']} | `{r['decl_name']}` | {r['module'].removeprefix('Mathlib.')} "
                 f"| {r['added_date']} | {'Y' if r['gold_commit_in_pin'] else ''} "
                 f"| {'Y' if r['full_name_anywhere'] else ''} "
                 f"| {'Y' if r['basename_decl_anywhere'] else ''} "
                 f"| {'Y' if r['basename_in_own_module'] else ''} |")
    L.append("")
    L.append("Reproduce: `python3 bench/analysis/fresh_exposure.py` "
             "(reads only the snapshot in `bench/analysis/snapshot_fresh_orig/`).\n")
    return "\n".join(L)


if __name__ == "__main__":
    main()
