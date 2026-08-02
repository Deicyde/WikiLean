#!/usr/bin/env python3
"""Bridge v2 — REVIEW-2 §8: format-failure repair sensitivity + cost columns.

Responds to external review 2 (docs/research/review/REVIEW-2.md §8):
  "N has 143/810 format failures — retrieval ability is confounded with
   output compliance. ... Add or redo: a deterministic extraction pass or
   identical format-repair turn for every arm; cost, latency, and
   calls/query in the results table."

Part 1 — ONE deterministic, maximally-lenient extraction pass, applied
SYMMETRICALLY to every arm's rows that have empty `ranked` (all four agent
arms, both benchmarks).  The pass never touches rows that already produced a
strict JSON array; it only tries to salvage the failures.  Source text =
concatenation of ALL assistant text blocks in the stored raw transcript
(runs/agent/<bench>/<arm>/<model>/<qid>.stream.jsonl.gz), falling back to the
`result` event's text.  Tiered harvest, first non-empty tier wins:

  T1 relaxed-array : any bracketed span that json-parses to a list of
                     strings, after mechanical repairs (single->double
                     quotes, trailing commas stripped, backticks stripped).
                     Last parseable span wins (same convention as the strict
                     extractor in bench/v2/run_agent.py::extract_ranked).
  T2 backtick      : `Token` spans whose contents look like a decl name.
  T3 dotted        : bare dotted identifiers (Foo.bar_baz) — every segment
                     must contain a letter; module-path spellings
                     (Mathlib.*, *.lean) are excluded.
  T4 compound      : single tokens with an internal underscore or a
                     camelCase transition (harvests names out of the grep /
                     rg patterns the N arm hallucinates as fake tool calls),
                     minus a fixed harness-vocabulary stoplist.

Order of first appearance, de-duplicated, capped at 10 — then scored by the
UNCHANGED metric code imported from bench/v2/score_retrieval.py (exact
full-name match; no suffix matching, per mathlib-decl-oracles).

Also reported: an ORACLE ceiling — every empty row counted as a rank-1 hit —
which upper-bounds how much of any arm's deficit format compliance could
possibly explain.

Part 2 — cost / latency / calls-per-query comparability table (REVIEW-2 §8
"cost, latency, and calls/query in the results table"; separate "agent" and
"retriever" blocks): per arm x benchmark, mean cost_usd / wall_s / tool
calls / turns / tokens_out straight from each row's transcript_stats, plus
the deterministic single-system wikibrain rows (runs/system/...) and the
published single-call anchors, which have no cost columns by nature.

Deterministic end to end: no sampling, no LLM, no network.  Inputs are the
frozen run rows + gzipped transcripts; running twice yields byte-identical
JSON (the md carries the same numbers).

Usage:  python3 bench/analysis/retrieval_repair.py
Writes: bench/analysis/retrieval_repair.json  (+ .md rendered from it)
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                             # bench
V2 = BENCH / "v2"
sys.path.insert(0, str(V2))
from score_retrieval import score_mpr, score_qr  # noqa: E402  (unchanged metrics)

RUNS = V2 / "runs" / "agent"
SYS_RUNS = V2 / "runs" / "system"
MODEL = "claude-sonnet-5"
ARMS = ["N", "F", "W", "WF"]
BENCHES = ["qr810", "mpr"]

# ---------------------------------------------------------------- extraction

SEG = r"[A-Za-z0-9_'!?«»]+"
DECL_DOTTED = re.compile(rf"\b[A-Za-z_][A-Za-z0-9_'!?]*(?:\.{SEG})+")
CAMEL_OR_SNAKE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9']*(?:_[A-Za-z0-9']+)+\b"      # snake compound
    r"|\b[A-Za-z]+[a-z0-9][A-Z][A-Za-z0-9']*\b")          # camel transition
BACKTICK = re.compile(r"`([^`\n]+)`")
BRACKET = re.compile(r"\[[^\[\]]*\]", re.S)

# Fixed harness-vocabulary stoplist for T4 (words the transcript scaffolding
# itself produces; applied identically to every arm).
STOP = {
    "BashTool", "bashtool", "ReadFile", "WriteFile", "ToolCall", "Tool_Call",
    "tool_call", "tool_use", "PowerShell", "powershell", "JavaScript",
    "output_mode", "files_with_matches", "ignoreCase", "maxdepth", "iname",
    "ipath", "include", "system_reminder", "session_id", "head_lines",
    "GitHub", "MathLib", "json_array", "e_g", "i_e",
}


def _segments_ok(name: str) -> bool:
    segs = name.split(".")
    return all(re.search(r"[A-Za-z]", s) for s in segs)


def _dotted_ok(name: str) -> bool:
    if name.startswith("Mathlib.") or name.endswith(".lean"):
        return False                     # module path / file, never a decl
    return _segments_ok(name)


def _try_array(span: str) -> list[str] | None:
    """json-parse a bracketed span, with mechanical repairs."""
    for cand in (span,
                 span.replace("`", ""),
                 re.sub(r",\s*\]", "]", span.replace("`", "")),
                 re.sub(r",\s*\]", "]", span.replace("`", "").replace("'", '"'))):
        try:
            arr = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and arr and all(isinstance(x, str) for x in arr):
            return [x.strip().strip("`") for x in arr if x.strip()]
    return None


def lenient_extract(text: str) -> tuple[list[str], str]:
    """The single symmetric pass.  Returns (names<=10, tier)."""
    def dedup(xs: list[str]) -> list[str]:
        seen, out = set(), []
        for x in xs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out[:10]

    # T1: last relaxed-parseable array (strict extractor's convention).
    for m in reversed(list(BRACKET.finditer(text))):
        arr = _try_array(m.group(0))
        if arr:
            return dedup(arr), "T1_array"
    # T2: backticked decl-shaped spans.
    bt = []
    for m in BACKTICK.finditer(text):
        tok = m.group(1).strip()
        if re.fullmatch(rf"[A-Za-z_][A-Za-z0-9_'!?]*(?:\.{SEG})*", tok) \
                and _dotted_ok(tok) and tok not in STOP:
            bt.append(tok)
    if bt:
        return dedup(bt), "T2_backtick"
    # T3: bare dotted identifiers.
    dotted = [m.group(0) for m in DECL_DOTTED.finditer(text)
              if _dotted_ok(m.group(0))]
    if dotted:
        return dedup(dotted), "T3_dotted"
    # T4: compound single tokens (snake / camel) minus the stoplist.
    comp = [m.group(0) for m in CAMEL_OR_SNAKE.finditer(text)
            if m.group(0) not in STOP]
    if comp:
        return dedup(comp), "T4_compound"
    return [], "none"


def assistant_text(stream_gz: Path) -> str:
    """All assistant text blocks, in order; fallback = result event text."""
    blocks, result_text = [], ""
    try:
        with gzip.open(stream_gz, "rt") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "assistant":
                    for blk in (ev.get("message") or {}).get("content", []) or []:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            blocks.append(blk.get("text") or "")
                elif ev.get("type") == "result" and isinstance(ev.get("result"), str):
                    result_text = ev["result"]
    except (OSError, EOFError):
        pass
    return "\n".join(blocks) if blocks else result_text


# ------------------------------------------------------------------- scoring

def load_rows(bench: str, arm: str) -> dict[str, dict]:
    d = RUNS / bench / arm / MODEL
    rows = {}
    for f in sorted(d.glob("*.json")):
        row = json.loads(f.read_text())
        row["_path"] = f
        rows[row["qid"]] = row
    return rows


def norm_list(xs: list[str]) -> list[str]:
    out = []
    for x in xs:
        n = (x or "").strip().strip("`")
        if n.startswith("decl:"):
            n = n.split(":", 2)[2]
        out.append(n)
    return out


def score(bench: str, ranked: dict[str, list[str]]) -> dict:
    return (score_qr if bench == "qr810" else score_mpr)(
        {q: norm_list(v) for q, v in ranked.items()})


def gold_map(bench: str) -> dict[str, set[str]]:
    """qid -> set of names that count as a hit (qr: the gold; mpr: any member
    of any group — used only for per-row 'gold recovered' bookkeeping)."""
    if bench == "qr810":
        from score_retrieval import qr_rows
        return {r["qid"]: {r["gold"]} for r in qr_rows()}
    from score_retrieval import mpr_rows
    return {r["qid"]: {d for g in r["groups"] for d in g} for r in mpr_rows()}


def main() -> int:
    out: dict = {"provenance": {
        "run_rows": "bench/v2/runs/agent/{qr810,mpr}/{N,F,W,WF}/claude-sonnet-5/*.json",
        "transcripts": "same dirs, *.stream.jsonl.gz (raw --output-format stream-json)",
        "gold": "bench/v2/data/MathlibQR.json + MathlibQR_shared171.json (fair-810) "
                "and bench/v2/data/MathlibMPR.json — pinned "
                "frenzymath/LeanSearch-v2@94f4888cbaf9, CC BY 4.0",
        "metrics": "imported unchanged from bench/v2/score_retrieval.py "
                   "(exact full-name match, R@10 / nDCG@10 / group-R@10)",
        "system_rows": "bench/v2/runs/system/{qr810,mpr}/wikibrain/*.json",
        "deterministic": "no sampling / LLM / network; reruns are byte-identical",
    }, "repair": {}, "deficit_decomposition": {}, "cost_table": {}}

    repairs_detail: dict[str, list[dict]] = {}
    for bench in BENCHES:
        out["repair"][bench] = {}
        golds = gold_map(bench)
        for arm in ARMS:
            rows = load_rows(bench, arm)
            strict = {q: r.get("ranked") or [] for q, r in rows.items()}
            lenient = dict(strict)
            oracle = dict(strict)
            tiers: dict[str, int] = {}
            n_empty = n_rep = n_gold = 0
            details = []
            n_timeout = 0
            for qid, r in rows.items():
                if strict[qid]:
                    continue
                n_empty += 1
                n_timeout += str(r.get("error", "")).startswith("timeout")
                text = assistant_text(Path(str(r["_path"]).replace(
                    ".json", ".stream.jsonl.gz")))
                names, tier = lenient_extract(text)
                tiers[tier] = tiers.get(tier, 0) + 1
                gold_hit = bool(set(norm_list(names)) & golds.get(qid, set()))
                if names:
                    n_rep += 1
                    lenient[qid] = names
                    n_gold += gold_hit
                # oracle ceiling: pretend the row was a rank-1 hit
                oracle[qid] = sorted(golds.get(qid, set()))[:1]
                details.append({"bench": bench, "arm": arm, "qid": qid,
                                "tier": tier, "n_names": len(names),
                                "gold_in_lenient_top10": gold_hit,
                                "names": names})
            repairs_detail.setdefault(bench, []).extend(details)  # type: ignore
            out["repair"][bench][arm] = {
                "n_rows": len(rows),
                "n_empty_ranked": n_empty,
                "n_empty_from_timeout": n_timeout,
                "n_repaired_nonempty": n_rep,
                "n_gold_recovered_top10": n_gold,
                "tier_histogram": dict(sorted(tiers.items())),
                "strict": score(bench, strict),
                "lenient": score(bench, lenient),
                "oracle_ceiling": score(bench, oracle),
            }

    # ---- deficit decomposition (headline: qr810, N vs each tooled arm) ----
    qr = out["repair"]["qr810"]
    n_strict, n_len = qr["N"]["strict"]["recall@10"], qr["N"]["lenient"]["recall@10"]
    n_oracle = qr["N"]["oracle_ceiling"]["recall@10"]
    dec = {"N_strict_R@10": n_strict, "N_lenient_R@10": n_len,
           "N_oracle_ceiling_R@10": n_oracle,
           "format_repair_gain_R@10": round(n_len - n_strict, 4),
           "format_oracle_bound_R@10": round(n_oracle - n_strict, 4),
           "vs": {}}
    for arm in ("F", "W", "WF"):
        a = qr[arm]["lenient"]["recall@10"]
        deficit = round(a - n_strict, 4)
        dec["vs"][arm] = {
            "arm_lenient_R@10": a,
            "N_deficit_strict": deficit,
            "closed_by_lenient_repair": round(n_len - n_strict, 4),
            "closed_by_lenient_repair_pct_of_deficit":
                round(100 * (n_len - n_strict) / deficit, 1) if deficit else None,
            "max_closable_by_any_format_fix_pct":
                round(100 * (n_oracle - n_strict) / deficit, 1) if deficit else None,
            "residual_retrieval_gap_even_at_oracle": round(a - n_oracle, 4),
        }
    out["deficit_decomposition"]["qr810"] = dec

    # -------------------------------------------------- cost / latency table
    for bench in BENCHES:
        rows_out = []
        for arm in ARMS:
            rows = load_rows(bench, arm)
            ts = [r.get("transcript_stats") or {} for r in rows.values()]
            costs = [t.get("cost_usd") for t in ts if t.get("cost_usd") is not None]
            walls = sorted(r.get("wall_s") for r in rows.values()
                           if r.get("wall_s") is not None)
            calls = [sum((t.get("tool_calls_by_name") or {}).values()) for t in ts]
            turns = [t.get("turns") for t in ts if t.get("turns") is not None]
            toks = [t.get("tokens_out") for t in ts if t.get("tokens_out") is not None]
            mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
            rows_out.append({
                "block": "agent", "system": f"{arm} (Sonnet agent)",
                "n": len(rows),
                "mean_cost_usd": mean(costs),
                "n_cost_missing": len(rows) - len(costs),
                "mean_wall_s": mean(walls),
                "median_wall_s": walls[len(walls) // 2] if walls else None,
                "mean_tool_calls": mean(calls),
                "mean_turns": mean(turns),
                "mean_tokens_out": mean(toks),
            })
        sysd = SYS_RUNS / bench / "wikibrain"
        n_sys = len(list(sysd.glob("*.json"))) if sysd.exists() else 0
        rows_out.append({
            "block": "retriever", "system": "wikibrain (deterministic pipeline)",
            "n": n_sys, "mean_cost_usd": 0.0, "n_cost_missing": 0,
            "mean_wall_s": None, "median_wall_s": None,
            "mean_tool_calls": 1, "mean_turns": None, "mean_tokens_out": 0,
            "note": "no LLM; wall not recorded in run rows",
        })
        rows_out.append({
            "block": "retriever", "system": "published anchors "
            "(TheoremGraph / LSv2+rerank / DIVER)",
            "n": None, "mean_cost_usd": None, "n_cost_missing": None,
            "mean_wall_s": None, "median_wall_s": None, "mean_tool_calls": 1,
            "mean_turns": None, "mean_tokens_out": None,
            "note": "single-call retrievers; cost/latency not published",
        })
        out["cost_table"][bench] = rows_out

    out["repaired_rows_detail"] = repairs_detail

    (HERE / "retrieval_repair.json").write_text(
        json.dumps(out, indent=1, sort_keys=False) + "\n")
    write_md(out)
    print("wrote", HERE / "retrieval_repair.json")
    print("wrote", HERE / "retrieval_repair.md")
    print(json.dumps(out["deficit_decomposition"], indent=1))
    return 0


# ------------------------------------------------------------------ markdown

def write_md(out: dict) -> None:
    L: list[str] = []
    A = L.append
    A("# Bridge v2 — format-failure repair sensitivity + cost columns "
      "(REVIEW-2 §8)\n")
    A("Generated by `bench/analysis/retrieval_repair.py` — deterministic "
      "(no sampling, no LLM, no network); inputs are the frozen run rows + "
      "raw transcripts under `bench/v2/runs/`. Metrics are imported "
      "unchanged from `bench/v2/score_retrieval.py`.\n")

    A("## 1. Symmetric lenient extraction\n")
    A("One tiered regex/name-harvest pass (T1 relaxed JSON array → T2 "
      "backticked names → T3 bare dotted identifiers → T4 compound tokens "
      "out of hallucinated grep patterns) over ALL assistant text in the "
      "stored transcript, applied identically to every arm's empty-`ranked` "
      "rows. Maximally lenient by design: it even harvests names the model "
      "only mentioned inside a fake tool-call it emitted instead of "
      "answering.\n")
    for bench in ("qr810", "mpr"):
        met = "recall@10" if bench == "qr810" else "group_recall@10"
        A(f"### {bench}\n")
        A("| arm | rows | empty | repaired≥1 | gold recovered | "
          f"strict {met} | lenient {met} | Δ | oracle ceiling |")
        A("|---|---|---|---|---|---|---|---|---|")
        for arm in ARMS:
            r = out["repair"][bench][arm]
            s, l = r["strict"][met], r["lenient"][met]
            A(f"| {arm} | {r['n_rows']} | {r['n_empty_ranked']} | "
              f"{r['n_repaired_nonempty']} | {r['n_gold_recovered_top10']} | "
              f"{s:.4f} | {l:.4f} | +{l - s:.4f} | "
              f"{r['oracle_ceiling'][met]:.4f} |")
        A("")
        if bench == "qr810":
            A("nDCG@10 strict → lenient: " + "; ".join(
                f"{arm} {out['repair'][bench][arm]['strict']['ndcg@10']:.4f}"
                f"→{out['repair'][bench][arm]['lenient']['ndcg@10']:.4f}"
                for arm in ARMS) + "\n")
        hist: dict[str, int] = {}
        for arm in ARMS:
            for t, n in out["repair"][bench][arm]["tier_histogram"].items():
                hist[t] = hist.get(t, 0) + n
        A(f"Tier histogram (all arms): {json.dumps(dict(sorted(hist.items())))}\n")
        to = {arm: out["repair"][bench][arm]["n_empty_from_timeout"]
              for arm in ARMS
              if out["repair"][bench][arm]["n_empty_from_timeout"]}
        if to:
            A("Empty rows that are 420 s hard timeouts (`error: timeout "
              "after 420s`), i.e. harness failures rather than format "
              f"failures: {json.dumps(to)}. Timed-out runs usually leave no "
              "final message, so most are unrepairable by any extraction.\n")

    A("## 2. How much of N's deficit was format compliance?\n")
    d = out["deficit_decomposition"]["qr810"]
    A(f"N strict R@10 **{d['N_strict_R@10']:.4f}** → lenient "
      f"**{d['N_lenient_R@10']:.4f}** (repair gain "
      f"+{d['format_repair_gain_R@10']:.4f}); oracle ceiling — every one of "
      f"N's empty rows counted as a rank-1 hit — "
      f"**{d['N_oracle_ceiling_R@10']:.4f}**.\n")
    A("| vs arm | arm lenient R@10 | N deficit (strict) | closed by lenient "
      "repair | % of deficit | max closable by ANY format fix | residual gap "
      "at oracle |")
    A("|---|---|---|---|---|---|---|")
    for arm, v in d["vs"].items():
        A(f"| {arm} | {v['arm_lenient_R@10']:.4f} | "
          f"{v['N_deficit_strict']:.4f} | "
          f"{v['closed_by_lenient_repair']:.4f} | "
          f"{v['closed_by_lenient_repair_pct_of_deficit']}% | "
          f"{v['max_closable_by_any_format_fix_pct']}% | "
          f"{v['residual_retrieval_gap_even_at_oracle']:.4f} |")
    A("")
    A("Reading: N's empty rows are dominated by hallucinated tool calls "
      "(the no-tools model emits a fake `grep`/`rg` command instead of an "
      "answer list), not by wrapper noncompliance — the lenient pass "
      "salvages names from most of them, but the names are usually search "
      "patterns, not the gold declaration. Even the oracle bound leaves the "
      "tooled arms ahead.\n")

    A("## 3. Cost / latency / calls per query (agents vs single-call "
      "retrievers)\n")
    A("From each row's `transcript_stats` (Claude-CLI totals; cost is the "
      "CLI's estimate under Max auth) and `wall_s`. Retriever block rows "
      "have no LLM cost by construction.\n")
    for bench in ("qr810", "mpr"):
        A(f"### {bench}\n")
        A("| block | system | n | mean $/query | mean wall s | median wall s "
          "| mean tool calls | mean turns | mean tokens out |")
        A("|---|---|---|---|---|---|---|---|---|")
        for r in out["cost_table"][bench]:
            fmt = lambda x, p="": ("–" if x is None else f"{x}{p}")  # noqa: E731
            cost = "–" if r["mean_cost_usd"] is None else f"{r['mean_cost_usd']:.4f}"
            A(f"| {r['block']} | {r['system']} | {fmt(r['n'])} | {cost} | "
              f"{fmt(r['mean_wall_s'])} | {fmt(r['median_wall_s'])} | "
              f"{fmt(r['mean_tool_calls'])} | {fmt(r['mean_turns'])} | "
              f"{fmt(r['mean_tokens_out'])} |")
        A("")
        miss = [f"{r['system']}: {r['n_cost_missing']}"
                for r in out["cost_table"][bench]
                if r.get("n_cost_missing")]
        if miss:
            A("Rows missing cost_usd (excluded from that mean): "
              + "; ".join(miss) + "\n")
    A("Caveat: the lenient pass can harvest a name the assistant merely "
      "echoed from the query; this is symmetric across arms and only makes "
      "the no-tools arm look better, so it is conservative for the paper's "
      "tooling claim.\n")
    (HERE / "retrieval_repair.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
