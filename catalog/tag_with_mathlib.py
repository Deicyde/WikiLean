#!/usr/bin/env python3
"""Tag pilot Wikipedia articles with matching Mathlib4 declarations.

Spawns Claude agents via claude-agent-sdk, authenticated against the user's
Claude Code login (Max plan). Each agent gets read-only access to the local
mathlib4 clone via Read/Grep/Glob, and returns a structured JSON tag.

Output is written incrementally to data/pilot_tagged.jsonl so the run is
resumable: re-running skips any titles already present in the output file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# IMPORTANT: pop the API key BEFORE importing the SDK, so the spawned `claude`
# subprocess uses Max-subscription auth (the local `claude login` session)
# rather than billing to whatever API account the key belongs to.
_popped_key = os.environ.pop("ANTHROPIC_API_KEY", None)

import requests
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "data" / "pilot.jsonl"
DEFAULT_OUT = HERE / "data" / "pilot_tagged.jsonl"
LEADS_CACHE = HERE / "data" / ".cache" / "leads.jsonl"
MATHLIB = Path("/Users/jack/Desktop/LEAN/mathlib4")

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_UA = (
    "WikiLean/0.1 (https://github.com/Deicyde/WikiLean; "
    "jack.mccarthy.1@stonybrook.edu)"
)
LEAD_BATCH = 20  # prop=extracts caps at 20 titles per call

SYSTEM_PROMPT = """\
You are a research assistant for the WikiLean project. For one Wikipedia
mathematics article, identify Mathlib4 declarations (defs, theorems, lemmas,
structures, classes, instances) that formalize its central concept.

Mathlib4 is the current working directory. Only look in `Mathlib/`.

Process:
  1. From the article, identify the central concept.
  2. Use Grep to find candidate declaration heads in Mathlib (e.g. `^def `,
     `^theorem `, `^class `, `^structure `, or the concept's canonical Mathlib
     spelling such as `MetricSpace`, `CauchySeq`).
  3. Use Read to verify candidates exist and match the concept.
  4. Report ONLY declarations you verified by grep/read. Do NOT invent names.
  5. Prefer 1-5 high-confidence decls over a long list of guesses.

OUTPUT FORMAT — your final reply must be ONLY one JSON object, no prose:

{
  "mathlib_decls": [
    {
      "decl": "<name in Mathlib, e.g. MetricSpace>",
      "module": "<dotted module path, e.g. Mathlib.Topology.MetricSpace.Basic>",
      "kind": "def" | "theorem" | "lemma" | "structure" | "class" | "instance" | "abbrev" | "inductive" | "other",
      "confidence": "high" | "medium" | "low",
      "evidence": "<relative path:line — short snippet you grepped>"
    }
  ],
  "primary_decl": "<single most central decl name, or null>",
  "notes": "<at most one sentence>",
  "no_match_reason": null
}

If nothing exists, return `mathlib_decls: []` and set `no_match_reason` to one
of: "not formalized", "too elementary", "not amenable to formalization",
"unclear scope", "other".
"""


# ---------------------------------------------------------------------------
# Lead fetching (MediaWiki prop=extracts, batched)
# ---------------------------------------------------------------------------

def fetch_leads(titles: list[str]) -> dict[str, str]:
    """Map title -> lead plaintext, using a JSONL cache to avoid refetching."""
    cache: dict[str, str] = {}
    LEADS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if LEADS_CACHE.exists():
        with LEADS_CACHE.open() as f:
            for line in f:
                rec = json.loads(line)
                cache[rec["title"]] = rec["lead"]
    pending = [t for t in titles if t not in cache]
    if not pending:
        return cache

    print(f"  fetching leads for {len(pending)} articles (batch={LEAD_BATCH})")
    s = requests.Session()
    s.headers.update({"User-Agent": WIKI_UA, "Accept-Encoding": "gzip"})
    t0 = time.time()
    with LEADS_CACHE.open("a") as out_f:
        for i in range(0, len(pending), LEAD_BATCH):
            chunk = pending[i : i + LEAD_BATCH]
            params = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "maxlag": "5",
            }
            for attempt in range(5):
                r = s.get(WIKI_API, params=params, timeout=60)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "5"))
                    print(f"    429; sleep {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                if (data.get("error") or {}).get("code") == "maxlag":
                    time.sleep(5)
                    continue
                break
            else:
                raise RuntimeError("lead fetch failed after retries")
            q = data.get("query", {})
            norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redir = {r["from"]: r["to"] for r in q.get("redirects", [])}
            pages = q.get("pages", [])
            by_resolved = {p.get("title"): p.get("extract") or "" for p in pages}
            for t in chunk:
                cur = norm.get(t, t)
                cur = redir.get(cur, cur)
                lead = by_resolved.get(cur, "") or ""
                cache[t] = lead
                out_f.write(json.dumps({"title": t, "lead": lead}, ensure_ascii=False) + "\n")
            out_f.flush()
            done = i + len(chunk)
            if done % 100 < LEAD_BATCH or done == len(pending):
                print(f"    leads: {done}/{len(pending)} ({time.time()-t0:.1f}s)", flush=True)
    return cache


# ---------------------------------------------------------------------------
# Agent orchestration
# ---------------------------------------------------------------------------

def build_user_prompt(article: dict, lead: str) -> str:
    return (
        f"Article: {article['title']}\n"
        f"Wikidata: {article.get('wikidata_qid', 'unknown')}\n"
        f"Class: {article.get('class')} / Importance: {article.get('importance')}\n"
        f"P31 (instance of): {article.get('p31') or []}\n\n"
        f"Lead:\n{lead or '(no lead available)'}\n\n"
        "Identify Mathlib declarations that formalize the central concept of "
        "this article.\nReply with ONLY the JSON object specified in the "
        "system prompt — no other text."
    )


def parse_json(text: str) -> dict | None:
    """Extract the first balanced JSON object from `text`."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start : i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None


async def tag_one(
    article: dict,
    lead: str,
    options: ClaudeAgentOptions,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        t0 = time.time()
        last_text = ""
        result_obj: ResultMessage | None = None
        n_tool_calls = 0
        try:
            async for msg in query(prompt=build_user_prompt(article, lead), options=options):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            last_text = b.text or last_text
                        elif isinstance(b, ToolUseBlock):
                            n_tool_calls += 1
                elif isinstance(msg, ResultMessage):
                    result_obj = msg
                    if msg.result:
                        last_text = msg.result
        except Exception as e:
            return {
                "title": article["title"],
                "wikidata_qid": article.get("wikidata_qid"),
                "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.time() - t0, 2),
            }

        parsed = parse_json(last_text)
        record: dict = {
            "title": article["title"],
            "wikidata_qid": article.get("wikidata_qid"),
            "class": article.get("class"),
            "importance": article.get("importance"),
        }
        if parsed is None:
            record["error"] = "no_json_in_result"
            record["raw_result"] = last_text[:2000]
        else:
            record.update(parsed)
        if result_obj is not None:
            record["agent_meta"] = {
                "num_turns": getattr(result_obj, "num_turns", None),
                "duration_ms": getattr(result_obj, "duration_ms", None),
                "n_tool_calls": n_tool_calls,
                "total_cost_usd": getattr(result_obj, "total_cost_usd", None),
                "is_error": getattr(result_obj, "is_error", None),
                "session_id": getattr(result_obj, "session_id", None),
            }
        record["elapsed_s"] = round(time.time() - t0, 2)
        return record


async def run(
    articles: list[dict],
    leads: dict[str, str],
    out_path: Path,
    concurrency: int,
    model: str,
    max_turns: int,
) -> int:
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=["Read", "Grep", "Glob"],
        cwd=str(MATHLIB),
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    t0 = time.time()
    n_done = 0
    n_err = 0
    cost = 0.0

    with out_path.open("a", encoding="utf-8") as out_f:

        async def worker(a: dict) -> None:
            nonlocal n_done, n_err, cost
            rec = await tag_one(a, leads.get(a["title"], ""), options, sem)
            async with lock:
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_f.flush()
                n_done += 1
                if rec.get("error"):
                    n_err += 1
                meta = rec.get("agent_meta") or {}
                if meta.get("total_cost_usd"):
                    cost += float(meta["total_cost_usd"])
                if n_done % 5 == 0 or n_done == len(articles):
                    elapsed = time.time() - t0
                    rate = n_done / elapsed if elapsed else 0
                    eta = (len(articles) - n_done) / rate if rate else 0
                    print(
                        f"  [{n_done}/{len(articles)}] last={rec['title']!r:40s} "
                        f"err={n_err} cost~${cost:.2f} eta={eta:.0f}s",
                        flush=True,
                    )

        await asyncio.gather(*(worker(a) for a in articles))
    print(
        f"\ndone — {n_done} tagged ({n_err} errors) in {time.time()-t0:.1f}s, "
        f"cost ~${cost:.2f}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--max-turns", type=int, default=20)
    ap.add_argument(
        "--include-humans",
        action="store_true",
        help="By default biographies (is_human=true) are skipped.",
    )
    args = ap.parse_args()

    if _popped_key:
        print("(unset ANTHROPIC_API_KEY for this process → using Max-plan auth)")
    if not MATHLIB.exists():
        print(f"ERROR: mathlib4 not found at {MATHLIB}", file=sys.stderr)
        return 1

    # Load input
    articles = [json.loads(line) for line in open(args.inp)]
    if not args.include_humans:
        articles = [a for a in articles if not a.get("is_human")]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done_titles: set[str] = set()
    if out_path.exists():
        for line in out_path.open():
            try:
                done_titles.add(json.loads(line)["title"])
            except (json.JSONDecodeError, KeyError):
                continue
    pending = [a for a in articles if a["title"] not in done_titles]
    if args.limit:
        pending = pending[: args.limit]
    print(
        f"input: {len(articles)} concept articles; "
        f"already tagged: {len(done_titles)}; pending: {len(pending)}"
    )
    if not pending:
        return 0

    print(f"\n[1/2] fetching leads")
    leads = fetch_leads([a["title"] for a in pending])

    print(f"\n[2/2] tagging via {args.concurrency} concurrent agents ({args.model})")
    return asyncio.run(
        run(pending, leads, out_path, args.concurrency, args.model, args.max_turns)
    )


if __name__ == "__main__":
    sys.exit(main())
