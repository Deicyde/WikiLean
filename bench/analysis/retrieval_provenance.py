#!/usr/bin/env python3
"""REVIEW-2 §5b — retrieval-provenance decomposition for QR-810 agent hits.

For every agent run row whose gold declaration lands in the final top-10
("hit"), classify how the gold NAME first entered the transcript:

  surfaced           (a) first appeared in a tool RESULT before the model ever
                         wrote it (sub-attributed to the surfacing tool; channel
                         brain = brain_bridge/search/cell/transfer/neighborhood/
                         filter/snippets + decl_exists rename SUGGESTIONS;
                         channel formal = loogle/decl_grep/decl_read)
  guessed_verified   (b) first WRITTEN by the model in a tool-call arg
                         (decl_exists names, decl_read name, grep pattern, ...)
                         and subsequently CONFIRMED (a decl_exists result says
                         exists:true for it, or any later tool result contains
                         it)
  written_unconfirmed    first written in a tool arg but never confirmed by any
                         tool result (expected rare)
  memory             (c) never appeared in any tool input or result — first
                         written in the final answer
  in_query               the gold full name already appears verbatim in the
                         benchmark query text (model was handed the name)
  undecidable            conservative-pass only: a truncated result_head
                         (cap 4000 chars) PRECEDES the first definite
                         occurrence, so "was it surfaced earlier?" cannot be
                         decided from the trace row alone

Two passes:
  conservative — tool_trace of bench/v2/runs/agent/qr810/<ARM>/<model>/<qid>.json
                 only (result_head truncated at 4000 chars -> undecidable rule)
  resolved     — same chronology rebuilt from the row's full transcript
                 (<qid>.stream.jsonl.gz, complete tool results; no truncation,
                 so no undecidable bucket)

Matching is EXACT full-name occurrence with identifier boundaries (prev/next
char not in [A-Za-z0-9_'.] or subscript digits) — never a bare suffix
(mathlib-decl-oracles). Non-ASCII golds (δ, π) are also searched in their
\\uXXXX JSON-escaped form since nested serializations may store them escaped.

Hit definition == bench/v2/score_retrieval.py: gold (MathlibQR.json full_name,
restricted to MathlibQR_shared171.json) in ranked[:10] after norm().

Deterministic — no sampling, no LLM, no seeds needed.

Usage: python3 bench/analysis/retrieval_provenance.py
Writes: bench/analysis/retrieval_provenance.json + .md
"""
from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
V2 = HERE.parent / "v2"
DATA = V2 / "data"
RUNS = V2 / "runs" / "agent" / "qr810"
ARMS = ["W", "WF", "F"]
RESULT_TRUNC = 4000                              # run_agent.py result_head cap

BOUND_BEFORE = r"(?<![A-Za-z0-9_'.₀-₉])"
# also reject a following JSON-escaped subscript (₀-₉), so
# "inductionOn₂" in an ascii-escaped serialization never matches
# gold "inductionOn"
BOUND_AFTER = r"(?![A-Za-z0-9_'.₀-₉])(?!\\u208)"

BRAIN_TOOLS = {"brain_bridge", "brain_search", "brain_cell", "brain_transfer",
               "brain_neighborhood", "brain_filter", "brain_snippets"}
FORMAL_TOOLS = {"loogle", "decl_grep", "decl_read"}
VERIFY_TOOLS = {"decl_exists"}


def norm(name: str) -> str:
    n = (name or "").strip().strip("`")
    if n.startswith("decl:"):
        n = n.split(":", 2)[2]
    return n


def tool_fn(name: str) -> str:
    """mcp__wikibrain__brain_bridge -> brain_bridge; Bash -> Bash."""
    return (name or "").split("__")[-1]


def tool_channel(name: str) -> str:
    fn = tool_fn(name)
    if fn in BRAIN_TOOLS:
        return "brain"
    if fn in FORMAL_TOOLS:
        return "formal"
    if fn in VERIFY_TOOLS:
        return "verify"
    return "other"


def gold_patterns(gold: str) -> list[re.Pattern]:
    variants = {gold}
    esc = json.dumps(gold, ensure_ascii=True)[1:-1]   # \uXXXX-escaped form
    variants.add(esc)
    return [re.compile(BOUND_BEFORE + re.escape(v) + BOUND_AFTER)
            for v in sorted(variants)]


def found(pats: list[re.Pattern], text: str) -> bool:
    return bool(text) and any(p.search(text) for p in pats)


def exists_true(gold: str, text: str) -> bool:
    """decl_exists result marks gold exists:true (handles nested escaping)."""
    flat = text.replace('\\"', '"')
    return f'"decl":"{gold}","exists":true' in flat


# ---------------------------------------------------------------- event models

class Ev:
    __slots__ = ("tool", "inp", "res", "truncated")

    def __init__(self, tool: str, inp: str, res: str, truncated: bool):
        self.tool, self.inp, self.res, self.truncated = tool, inp, res, truncated


def events_from_trace(row: dict) -> list[Ev]:
    evs = []
    for t in row.get("tool_trace") or []:
        head = t.get("result_head") or ""
        chars = t.get("result_chars") or 0
        evs.append(Ev(t.get("name") or "", t.get("input") or "", head,
                      chars > len(head)))
    return evs


def events_from_stream(path: Path) -> list[Ev] | None:
    """Rebuild the full chronology (untruncated results) from the stream."""
    if not path.exists():
        return None
    uses: dict[str, tuple[int, str, str]] = {}   # id -> (order, tool, input)
    results: dict[str, str] = {}
    order = 0
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        uses[c.get("id") or f"anon{order}"] = (
                            order, c.get("name") or "",
                            json.dumps(c.get("input") or {},
                                       ensure_ascii=False))
                        order += 1
                    elif c.get("type") == "tool_result":
                        rid = c.get("tool_use_id") or ""
                        results[rid] = json.dumps(c.get("content"),
                                                  ensure_ascii=False)
    except OSError:
        return None
    evs = [Ev(tool, inp, results.get(uid, ""), False)
           for uid, (o, tool, inp) in sorted(uses.items(), key=lambda kv: kv[1][0])]
    return evs


# ------------------------------------------------------------------ classifier

def classify(gold: str, query: str, evs: list[Ev]) -> dict:
    """Chronological first-entry classification (see module docstring)."""
    pats = gold_patterns(gold)
    if found(pats, query):
        return {"cls": "in_query", "tool": None}

    first_kind = first_i = first_tool = None      # first DEFINITE occurrence
    earliest_trunc = None                          # earliest unreadable result
    for i, ev in enumerate(evs):
        if first_kind is None and found(pats, ev.inp):
            first_kind, first_i, first_tool = "input", i, ev.tool
        if first_kind is None and found(pats, ev.res):
            first_kind, first_i, first_tool = "result", i, ev.tool
        if first_kind is not None:
            break
        if ev.truncated and not found(pats, ev.res) and earliest_trunc is None:
            earliest_trunc = i

    if first_kind == "result":
        # Even if an earlier truncated result might also have carried it, the
        # class is surfaced either way; attribution uses the first readable one.
        return {"cls": "surfaced", "tool": tool_fn(first_tool),
                "channel": tool_channel(first_tool),
                "attribution_tentative": earliest_trunc is not None}

    if first_kind == "input":
        if earliest_trunc is not None and earliest_trunc < first_i:
            return {"cls": "undecidable", "tool": None,
                    "reason": f"truncated result at event {earliest_trunc} "
                              f"precedes first model-written occurrence at "
                              f"event {first_i}"}
        confirmed = conf_undecidable = False
        etrue = any(exists_true(gold, ev.res) for ev in evs[first_i:])
        for j in range(first_i, len(evs)):
            if found(pats, evs[j].res) or exists_true(gold, evs[j].res):
                confirmed = True
                break
            if evs[j].truncated:
                conf_undecidable = True
        if confirmed:
            return {"cls": "guessed_verified", "tool": tool_fn(first_tool),
                    "channel": tool_channel(first_tool),
                    "exists_true": etrue}
        return {"cls": "written_unconfirmed", "tool": tool_fn(first_tool),
                "confirmation_truncation": conf_undecidable}

    # never definitely occurred in any input or readable result
    if earliest_trunc is not None:
        return {"cls": "undecidable", "tool": None,
                "reason": f"never seen in trace but truncated result at event "
                          f"{earliest_trunc} could have surfaced it"}
    return {"cls": "memory", "tool": None}


# ------------------------------------------------------------------------ main

def qr_gold() -> dict[str, dict]:
    qr = json.loads((DATA / "MathlibQR.json").read_text())
    shared = set(json.loads((DATA / "MathlibQR_shared171.json").read_text())
                 ["shared_declarations"])
    out = {}
    for r in qr:
        if r["full_name"] not in shared:
            continue
        for f in ["q1a_lean", "q1b_latex", "q1c_natural",
                  "q2_slogan", "q3_nickname", "q4_special_case"]:
            if (r.get(f) or "").strip():
                out[f"{r['id']}__{f}"] = {"gold": r["full_name"], "style": f}
    return out


def main() -> int:
    gold_map = qr_gold()
    out: dict = {"provenance": {
        "rows": "bench/v2/runs/agent/qr810/{W,WF,F}/claude-sonnet-5/<qid>.json "
                "(tool_trace, result_head cap 4000) + <qid>.stream.jsonl.gz "
                "(full tool results)",
        "gold": "bench/v2/data/MathlibQR.json restricted to "
                "MathlibQR_shared171.json (fair-810), exact-name top-10 hit "
                "as in bench/v2/score_retrieval.py",
    }, "arms": {}}
    per_row: list[dict] = []

    for arm in ARMS:
        arm_dir = RUNS / arm
        rows = sorted(arm_dir.rglob("*.json"))
        n_rows = n_hits = 0
        cons = Counter()
        reso = Counter()
        cons_by_style: dict[str, Counter] = defaultdict(Counter)
        reso_by_style: dict[str, Counter] = defaultdict(Counter)
        surf_tool = Counter()          # resolved surfacing tool
        surf_channel = Counter()
        guess_tool = Counter()         # resolved first-written tool
        guess_exists_true = 0          # b-rows with a decl_exists exists:true
        guess_saw_reject = 0           # b-rows where the oracle ALSO rejected
                                       # at least one candidate (exists:false)
        agree = disagree = 0
        undec_resolved_to = Counter()

        for p in rows:
            row = json.loads(p.read_text())
            qid = row.get("qid") or p.stem
            meta = gold_map.get(qid)
            if not meta:
                continue
            n_rows += 1
            gold = meta["gold"]
            ranked = [norm(x) for x in (row.get("ranked") or [])][:10]
            if gold not in ranked:
                continue
            n_hits += 1
            style = meta["style"]
            query = row.get("query") or ""

            c = classify(gold, query, events_from_trace(row))
            cons[c["cls"]] += 1
            cons_by_style[style][c["cls"]] += 1

            sev = events_from_stream(p.parent / (p.stem + ".stream.jsonl.gz"))
            if sev is None:
                r = dict(c)                       # no stream: keep conservative
                r["stream_missing"] = True
            else:
                r = classify(gold, query, sev)
            reso[r["cls"]] += 1
            reso_by_style[style][r["cls"]] += 1
            if r["cls"] == "surfaced":
                surf_tool[r["tool"]] += 1
                surf_channel[r["channel"]] += 1
            elif r["cls"] == "guessed_verified":
                guess_tool[r["tool"]] += 1
                if r.get("exists_true"):
                    guess_exists_true += 1
                if sev and any(tool_fn(ev.tool) == "decl_exists" and
                               '"exists":false' in ev.res.replace('\\"', '"')
                               for ev in sev):
                    guess_saw_reject += 1
            if c["cls"] == "undecidable":
                undec_resolved_to[r["cls"]] += 1
            elif c["cls"] == r["cls"]:
                agree += 1
            else:
                disagree += 1

            per_row.append({"arm": arm, "qid": qid, "gold": gold,
                            "style": style, "conservative": c["cls"],
                            "resolved": r["cls"],
                            "tool": r.get("tool"), "channel": r.get("channel"),
                            "exists_true": r.get("exists_true")})

        def frac(cnt: Counter) -> dict:
            return {k: {"n": v, "frac": round(v / n_hits, 4)}
                    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])}

        out["arms"][arm] = {
            "n_rows": n_rows, "n_hits": n_hits,
            "hit_rate": round(n_hits / n_rows, 4) if n_rows else None,
            "conservative": frac(cons),
            "resolved": frac(reso),
            "resolved_surfaced_by_tool": dict(surf_tool.most_common()),
            "resolved_surfaced_by_channel": dict(surf_channel.most_common()),
            "resolved_guessed_first_written_in": dict(guess_tool.most_common()),
            "resolved_guessed_with_exists_true": guess_exists_true,
            "resolved_guessed_with_some_rejection": guess_saw_reject,
            "conservative_undecidable_resolved_to":
                dict(undec_resolved_to.most_common()),
            "trace_vs_stream_agreement": {
                "agree": agree, "disagree": disagree,
                "undecidable_resolved": sum(undec_resolved_to.values())},
            "resolved_by_style": {s: dict(c.most_common())
                                  for s, c in sorted(reso_by_style.items())},
            "conservative_by_style": {s: dict(c.most_common())
                                      for s, c in sorted(cons_by_style.items())},
        }

    # N-arm context: no tools at all -> every hit is memory by construction
    n_dir = RUNS / "N"
    n_rows = n_hits = 0
    for p in sorted(n_dir.rglob("*.json")):
        row = json.loads(p.read_text())
        meta = gold_map.get(row.get("qid") or p.stem)
        if not meta:
            continue
        n_rows += 1
        if meta["gold"] in [norm(x) for x in (row.get("ranked") or [])][:10]:
            n_hits += 1
    out["arms"]["N_context"] = {
        "n_rows": n_rows, "n_hits": n_hits,
        "note": "N has zero tool calls; all hits are memory by construction"}

    out["per_row"] = per_row
    (HERE / "retrieval_provenance.json").write_text(
        json.dumps(out, indent=1) + "\n")
    write_md(out)
    print(json.dumps({a: {k: v for k, v in d.items()
                          if k in ("n_hits", "conservative", "resolved")}
                      for a, d in out["arms"].items() if a != "N_context"},
                     indent=1))
    return 0


def write_md(out: dict) -> None:
    L = ["# Retrieval provenance — how the gold name entered the transcript",
         "",
         "REVIEW-2 §5b. For every QR-810 agent hit (gold in final top-10), the",
         "chronologically FIRST entry of the gold declaration name:",
         "**surfaced** (a: appeared in a tool result before the model wrote it),",
         "**guessed_verified** (b: model wrote it in a tool arg, then confirmed),",
         "**memory** (c: never in any tool input/result), plus in_query /",
         "written_unconfirmed / undecidable (truncated-trace conservative pass).",
         "",
         f"Data: `{out['provenance']['rows']}`",
         f"Gold/hit: `{out['provenance']['gold']}`",
         "",
         "Conservative pass = tool_trace only (4000-char result_head; a",
         "truncated result preceding the first definite occurrence =>",
         "undecidable). Resolved pass = full tool results from the",
         "stream transcripts (no truncation).", ""]
    cats = ["surfaced", "guessed_verified", "memory", "in_query",
            "written_unconfirmed", "undecidable"]
    for pas in ("conservative", "resolved"):
        L += [f"## {pas.capitalize()} pass", "",
              "| arm | hits | " + " | ".join(cats) + " |",
              "|---|---|" + "---|" * len(cats)]
        for arm in ARMS:
            d = out["arms"][arm]
            cells = []
            for c in cats:
                e = d[pas].get(c)
                cells.append(f"{e['n']} ({e['frac']:.1%})" if e else "0")
            L.append(f"| {arm} | {d['n_hits']} | " + " | ".join(cells) + " |")
        L.append("")
    L += ["## Resolved detail", ""]
    for arm in ARMS:
        d = out["arms"][arm]
        L += [f"### {arm}",
              f"- surfaced by tool: "
              f"`{json.dumps(d['resolved_surfaced_by_tool'])}`",
              f"- surfaced by channel: "
              f"`{json.dumps(d['resolved_surfaced_by_channel'])}`",
              f"- guessed_verified first written in: "
              f"`{json.dumps(d['resolved_guessed_first_written_in'])}`",
              f"- guessed_verified with a decl_exists exists:true for the "
              f"gold: {d['resolved_guessed_with_exists_true']}",
              f"- guessed_verified where the oracle also REJECTED >=1 "
              f"candidate (exists:false): "
              f"{d['resolved_guessed_with_some_rejection']}",
              f"- conservative undecidable resolved to: "
              f"`{json.dumps(d['conservative_undecidable_resolved_to'])}`",
              f"- trace-vs-stream agreement: "
              f"`{json.dumps(d['trace_vs_stream_agreement'])}`", ""]
        L += ["| style | " + " | ".join(cats) + " |",
              "|---|" + "---|" * len(cats)]
        for s, cnt in d["resolved_by_style"].items():
            L.append(f"| {s} | " + " | ".join(str(cnt.get(c, 0))
                                              for c in cats) + " |")
        L.append("")
    nc = out["arms"]["N_context"]
    L += [f"N context: {nc['n_hits']}/{nc['n_rows']} hits, {nc['note']}.", ""]
    (HERE / "retrieval_provenance.md").write_text("\n".join(L))


if __name__ == "__main__":
    import sys
    sys.exit(main())
