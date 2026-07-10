#!/usr/bin/env python3
"""Nightly Brain agent team (BRAIN v2 axis 4) — propose-only, budget-gated.

Two roles this version (docs/BRAIN-V2.md "Nightly brain sync"):

  cartographer  Candidate generation is DETERMINISTIC string matching: external
                pages with no CC0 qid (catalog/data/external/<db>_pages.jsonl)
                whose title exactly-or-nearly matches a Brain concept label.
                The agent only judges same-concept-or-not given the page
                title/snippet and the concept label/description. Accepted pairs
                → brain/proposals/ext_anchor_<date>.jsonl rows
                {"action":"xref","qid","xref":{"db","id"},"reason",...}.
  skeptic       Adversarial second opinion over ext_anchor rows that lack a
                verdict → <shard>.jsonl.verified.jsonl rows (the base row
                echoed + verdict/verify_note), the same overlay contract
                fold_proposals.py reads for every other proposal family.

This script NEVER writes brain/data/ — fold_proposals.py (deterministic
verifier) is the only gate; its action:"xref" handler folds verified rows
into brain/data/ext_anchor_links.jsonl for build_common to consume.

Idempotent: (db, page-id, qid) pairs already present in ANY ext_anchor shard
are never re-proposed; pairs the cartographer judged NOT-same-concept persist
in brain/proposals/.ext_anchor_rejected_cache.jsonl and are never re-judged;
skeptic rows already echoed in a .verified.jsonl are never re-judged. Runs to
completion with 0 candidates. --dry-run prints the work plan without writing
anything and without importing the SDK.

Run with the venv that has claude-agent-sdk:
    catalog/.venv/bin/python3 brain/sync_agents.py --dry-run --limit 5
    catalog/.venv/bin/python3 brain/sync_agents.py --budget-tokens 500000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# Pop the API key BEFORE any SDK import (the import is lazy, inside _run_agent)
# so the spawned `claude` subprocess uses the Max-subscription login rather
# than billing an API account — same contract as site/batch_annotate.py.
_popped_key = None
if os.environ.get("WIKILEAN_KEEP_API_KEY") != "1":
    _popped_key = os.environ.pop("ANTHROPIC_API_KEY", None)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PROPOSALS = HERE / "proposals"
NODES = HERE / "data" / "nodes.jsonl"
EXTERNAL = REPO / "catalog" / "data" / "external"
DESCRIPTIONS = REPO / "catalog" / "data" / "wikidata_descriptions.json"

MODEL = os.environ.get("WIKILEAN_BRAIN_AGENT_MODEL", "claude-sonnet-5")
DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
SHARD = PROPOSALS / f"ext_anchor_{DATE}.jsonl"
# Judged-NOT-same-concept pairs (cartographer rejections). Dotfile so neither
# fold_proposals' *.jsonl glob nor the ext_anchor_* shard globs pick it up.
REJECTED_CACHE = PROPOSALS / ".ext_anchor_rejected_cache.jsonl"
CHUNK = 12          # candidate pairs per agent call
MAX_CONCURRENCY = 4
ABORT_AFTER = 5     # consecutive window-exhaustion errors → exit 3 (retryable)

_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Deterministic candidate generation
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    """Match key: strip accents, drop a trailing parenthetical, unify
    separators. Only COMBINING marks are dropped (Lindelöf→Lindelof) — other
    non-ascii stays, so '∞-module' can never collide with plain 'module'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("–", "-").replace("—", "-")
    s = _PAREN.sub("", s)
    s = s.replace("+", " ").replace("_", " ").replace("-", " ").replace("'", "")
    return _WS.sub(" ", s).casefold().strip()


def near_keys(s: str) -> set[str]:
    """'exactly-or-nearly': the normalized form plus a plural/singular twin."""
    n = norm(s)
    if not n:
        return set()
    keys = {n}
    keys.add(n[:-1] if n.endswith("s") else n + "s")
    return keys


def iter_jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                if "_meta" not in r:
                    yield r


def load_concepts() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """(qid -> {label, slug, description}, normalized name -> [qids])."""
    descriptions: dict[str, str] = {}
    if DESCRIPTIONS.exists():
        try:
            raw = json.loads(DESCRIPTIONS.read_text())
            descriptions = {q: (d if isinstance(d, str) else d.get("description", ""))
                            for q, d in raw.items()}
        except (json.JSONDecodeError, AttributeError):
            pass
    concepts: dict[str, dict] = {}
    index: dict[str, list[str]] = {}
    for n in iter_jsonl(NODES):
        if n.get("type") != "concept":
            continue
        qid = n["id"]
        concepts[qid] = {"label": n.get("label") or "", "slug": n.get("slug"),
                         "description": descriptions.get(qid, "")}
        for name in (n.get("label"), n.get("slug")):
            # a leading separator marks an upstream ascii-stripping artifact
            # ('Σ-algebra' stored as slug '-algebra') — indexing it would
            # collide with the plain word
            if not name or name[0] in "-+_":
                continue
            for k in near_keys(name):
                bucket = index.setdefault(k, [])
                if qid not in bucket:
                    bucket.append(qid)
    return concepts, index


def already_proposed() -> set[tuple[str, str, str]]:
    """(db, page-id, qid) pairs in any ext_anchor shard (base rows) — the
    idempotency set; includes rows a fold later rejected (never re-burn)."""
    seen: set[tuple[str, str, str]] = set()
    for f in sorted(PROPOSALS.glob("ext_anchor_*.jsonl")):
        if f.name.endswith(".verified.jsonl"):
            continue
        for r in iter_jsonl(f):
            x = r.get("xref") or {}
            if r.get("qid") and x.get("db") and x.get("id"):
                seen.add((x["db"], str(x["id"]), r["qid"]))
    return seen


def rejected_pairs() -> set[tuple[str, str, str]]:
    """(db, page-id, qid) pairs the cartographer already judged NOT the same
    concept (REJECTED_CACHE rows {db,id,qid,judged_at,run}). Excluded from
    candidate generation so the same false candidates are not re-judged every
    night and the frontier cannot stall on them."""
    seen: set[tuple[str, str, str]] = set()
    if REJECTED_CACHE.exists():
        for r in iter_jsonl(REJECTED_CACHE):
            if r.get("db") and r.get("id") and r.get("qid"):
                seen.add((r["db"], str(r["id"]), r["qid"]))
    return seen


def record_rejections(pairs: list[tuple[str, str, str]]) -> int:
    """Merge judged-negative (db,id,qid) pairs into REJECTED_CACHE — read,
    dedupe (first write wins), rewrite sorted by key, atomic tmp+rename.
    Returns pairs newly added."""
    if not pairs:
        return 0
    merged: dict[tuple[str, str, str], dict] = {}
    if REJECTED_CACHE.exists():
        for r in iter_jsonl(REJECTED_CACHE):
            k = (r.get("db") or "", str(r.get("id") or ""), r.get("qid") or "")
            merged.setdefault(k, r)
    judged_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for db, pid, qid in pairs:
        k = (db, pid, qid)
        if k not in merged:
            merged[k] = {"db": db, "id": pid, "qid": qid,
                         "judged_at": judged_at, "run": f"cartographer-{DATE}"}
            added += 1
    if not added:
        return 0
    REJECTED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REJECTED_CACHE.with_suffix(REJECTED_CACHE.suffix + ".tmp")
    with tmp.open("w") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k], ensure_ascii=False) + "\n")
    tmp.rename(REJECTED_CACHE)
    return added


def gen_candidates(limit: int) -> list[dict]:
    if not NODES.exists():
        print(f"NOTE: {NODES} missing — no concept index, 0 candidates", file=sys.stderr)
        return []
    concepts, index = load_concepts()
    # same (db, page-id, qid) dedup key as the accept path: skip pairs already
    # proposed in any shard AND pairs the cartographer already judged negative
    skip = already_proposed() | rejected_pairs()
    cands: list[dict] = []
    if not EXTERNAL.exists():
        print(f"NOTE: {EXTERNAL} missing — no external pages yet, 0 candidates",
              file=sys.stderr)
        return []
    for pages_file in sorted(EXTERNAL.glob("*_pages.jsonl")):
        for row in iter_jsonl(pages_file):
            if row.get("qid"):
                continue  # already anchored by a CC0 Wikidata property
            db, pid = row.get("db"), str(row.get("id") or "")
            title = row.get("title") or ""
            if not (db and pid and title):
                continue
            qids: set[str] = set()
            for name in [title] + list(row.get("aliases") or []):
                for k in near_keys(name):
                    qids.update(index.get(k, ()))
            for qid in sorted(qids):
                if (db, pid, qid) in skip:
                    continue
                c = concepts[qid]
                cands.append({
                    "db": db, "id": pid, "title": title,
                    "url": row.get("url"), "snippet": row.get("snippet") or "",
                    "qid": qid, "qid_label": c["label"],
                    "qid_description": c["description"],
                })
    cands.sort(key=lambda c: (c["db"], c["id"], c["qid"]))
    return cands[:limit] if limit is not None and limit >= 0 else cands


def skeptic_backlog() -> dict[Path, list[dict]]:
    """ext_anchor shard -> base rows with no echoed verdict in its overlay."""
    out: dict[Path, list[dict]] = {}
    for f in sorted(PROPOSALS.glob("ext_anchor_*.jsonl")):
        if f.name.endswith(".verified.jsonl"):
            continue
        vf = Path(str(f) + ".verified.jsonl")
        echoed: set[tuple] = set()
        if vf.exists():
            for r in iter_jsonl(vf):
                x = r.get("xref") or {}
                echoed.add((x.get("db"), str(x.get("id")), r.get("qid")))
        pending = []
        for r in iter_jsonl(f):
            x = r.get("xref") or {}
            if not (x.get("db") and x.get("id") and r.get("qid")):
                continue  # malformed row — never dispatch, never crash
            if (x.get("db"), str(x.get("id")), r.get("qid")) not in echoed:
                pending.append(r)
        if pending:
            out[f] = pending
    return out


# ---------------------------------------------------------------------------
# Agent plumbing (SDK imported lazily so --dry-run needs no SDK)
# ---------------------------------------------------------------------------

def parse_json_object(text: str) -> dict | None:
    """First balanced {...} in text (same parser as site/batch_annotate.py)."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
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
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


CARTOGRAPHER_SYSTEM = """\
You are the CARTOGRAPHER in the WikiLean Brain nightly sync. You receive
candidate pairs, each joining an external math-database page (db, id, title,
snippet, url) to a Wikidata concept (qid, label, description). The pairs come
from deterministic title matching; your ONLY job is to judge, per pair, whether
the page is about the SAME mathematical concept as the QID — not a broader
field, not a narrower special case, not a merely related notion, not a
same-named concept from a different area.

Judge conservatively: when unsure, answer false. A wrong anchor pollutes the
graph; a missed one costs nothing.

OUTPUT — your final reply must be ONLY one JSON object, no prose. Echo every
input pair exactly once, in the same order, db/id/qid byte-identical:
{"rows": [{"db": "…", "id": "…", "qid": "Q…", "same_concept": true,
           "reason": "<one sentence>"}]}
"""

SKEPTIC_SYSTEM = """\
You are the SKEPTIC in the WikiLean Brain nightly sync — the adversarial
second opinion on proposed external-page ↔ Wikidata-concept anchors. For each
proposed row (page db/id/title vs concept qid/label/description, plus the
proposer's reason), try to REFUTE it: is the page actually about a broader
field, a narrower case, a homonym, or a different-area concept? Accept only
anchors you cannot refute.

OUTPUT — your final reply must be ONLY one JSON object, no prose. Echo every
input row exactly once, in the same order, db/id/qid byte-identical:
{"rows": [{"db": "…", "id": "…", "qid": "Q…", "verdict": "accept"|"reject",
           "verify_note": "<one sentence>"}]}
"""


async def _run_agent(system: str, user: str, state: dict) -> dict | None:
    """One SDK call, no tools (pure judgment). Returns parsed JSON or None;
    accumulates tokens into state and counts window-exhaustion errors."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ResultMessage, TextBlock, query)
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=system,
        allowed_tools=[],
        cwd=str(HERE),
        permission_mode="bypassPermissions",
        max_turns=4,
    )
    last_text = ""
    result_obj = None
    try:
        async for msg in query(prompt=user, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        last_text = b.text or last_text
            elif isinstance(msg, ResultMessage):
                result_obj = msg
                if msg.result:
                    last_text = msg.result
    except Exception as e:  # surface the CLI's real cause (see batch_annotate)
        detail = getattr(result_obj, "result", None) if result_obj else None
        err = f"{detail or e}"
        low = err.lower()
        if ("error result: success" in low or "rate" in low
                or "limit" in low or "overloaded" in low):
            state["consec_err"] += 1
            if state["consec_err"] >= ABORT_AFTER:
                state["abort"] = True
        print(f"  agent_error: {err[:300]}", file=sys.stderr)
        return None
    state["consec_err"] = 0
    usage = getattr(result_obj, "usage", None) if result_obj else None
    if isinstance(usage, dict):
        state["tokens"] += (usage.get("input_tokens") or 0) + \
                           (usage.get("output_tokens") or 0)
    return parse_json_object(last_text)


def _chunks(rows: list, n: int) -> list[list]:
    return [rows[i:i + n] for i in range(0, len(rows), n)]


async def _judge(role_system: str, payloads: list[list[dict]], budget: int,
                 concurrency: int, state: dict) -> list[dict]:
    """Run one role over its chunks (≤4 concurrent, budget-gated). Returns the
    agent rows, filtered to (db,id,qid) triples actually dispatched — snippet
    text is a prompt-injection surface, so an agent can never mint a pair we
    did not ask about."""
    sem = asyncio.Semaphore(concurrency)
    out: list[dict] = []
    lock = asyncio.Lock()

    async def worker(chunk: list[dict]):
        async with sem:
            if state["abort"] or state["tokens"] >= budget:
                return
            allowed = {(c["db"], c["id"], c["qid"]) for c in chunk}
            user = ("Candidate pairs (JSON):\n"
                    + json.dumps(chunk, ensure_ascii=False)
                    + "\n\nJudge each per the system prompt. "
                      "Reply with ONLY the JSON object.")
            res = await _run_agent(role_system, user, state)
            rows = (res or {}).get("rows")
            if not isinstance(rows, list):
                return
            async with lock:
                for r in rows:
                    if (isinstance(r, dict)
                            and (r.get("db"), str(r.get("id")), r.get("qid")) in allowed):
                        out.append(r)

    await asyncio.gather(*(worker(c) for c in payloads))
    return out


# ---------------------------------------------------------------------------
# Atomic shard writes (merge + dedupe + sort — deterministic row order)
# ---------------------------------------------------------------------------

def _row_key(r: dict) -> tuple:
    x = r.get("xref") or {}
    return (x.get("db") or "", str(x.get("id") or ""), r.get("qid") or "")


def write_shard(path: Path, new_rows: list[dict]) -> int:
    """Merge new rows into the shard, keyed by (db,id,qid); first write wins
    (idempotent re-runs). Atomic tmp+rename. Returns rows added."""
    merged: dict[tuple, dict] = {}
    if path.exists():
        for r in iter_jsonl(path):
            merged[_row_key(r)] = r
    added = 0
    for r in new_rows:
        k = _row_key(r)
        if k not in merged:
            merged[k] = r
            added += 1
    if not added and path.exists():
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k], ensure_ascii=False) + "\n")
    tmp.rename(path)
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args) -> int:
    concurrency = max(1, min(MAX_CONCURRENCY, args.concurrency))
    budget = args.budget_tokens
    state = {"tokens": 0, "consec_err": 0, "abort": False}
    proposer_tag = "sonnet" if "sonnet" in MODEL else MODEL
    t0 = time.time()

    # --- cartographer -------------------------------------------------------
    cands = gen_candidates(args.limit)
    n_anchored = 0
    if cands and "cartographer" in args.roles:
        judged = await _judge(
            CARTOGRAPHER_SYSTEM,
            _chunks(cands, CHUNK), budget, concurrency, state)
        by_key = {(c["db"], c["id"], c["qid"]): c for c in cands}
        rows = []
        rejected: list[tuple[str, str, str]] = []
        for r in sorted(judged, key=lambda r: (r["db"], str(r["id"]), r["qid"])):
            if r.get("same_concept") is not True:
                # judged and NOT accepted — cache the pair so it is never
                # re-generated as a candidate (frontier stall otherwise)
                rejected.append((r["db"], str(r["id"]), r["qid"]))
                continue
            c = by_key[(r["db"], str(r["id"]), r["qid"])]
            rows.append({
                "action": "xref", "qid": c["qid"], "qid_label": c["qid_label"],
                "xref": {"db": c["db"], "id": c["id"]},
                "title": c["title"], "url": c["url"],
                "reason": str(r.get("reason") or "")[:400],
                "confidence": "medium",
                "proposer": f"{proposer_tag}-cartographer-{DATE}",
            })
        n_anchored = write_shard(SHARD, rows)
        n_rejected = record_rejections(rejected)
        print(f"cartographer: {len(cands)} candidates → {len(rows)} same-concept "
              f"→ {n_anchored} new rows in {SHARD.name}; "
              f"{len(rejected)} judged-negative → {n_rejected} new rows in "
              f"{REJECTED_CACHE.name}")
    else:
        print(f"cartographer: {len(cands)} candidates — "
              + ("role disabled" if cands else "nothing to do"))

    # --- skeptic (covers tonight's new rows too) -----------------------------
    if "skeptic" in args.roles and not state["abort"]:
        backlog = skeptic_backlog()
        n_verdicts = 0
        for shard, pending in backlog.items():
            if state["abort"] or state["tokens"] >= budget:
                print(f"skeptic: budget/window stop before {shard.name} "
                      f"({state['tokens']:,}/{budget:,} tokens)")
                break
            payload = [{"db": r["xref"]["db"], "id": r["xref"]["id"],
                        "qid": r["qid"], "qid_label": r.get("qid_label"),
                        "title": r.get("title"), "url": r.get("url"),
                        "proposer_reason": r.get("reason")} for r in pending]
            judged = await _judge(SKEPTIC_SYSTEM, _chunks(payload, CHUNK),
                                  budget, concurrency, state)
            base = {_row_key(r): r for r in pending}
            vrows = []
            for r in judged:
                k = (r["db"], str(r["id"]), r["qid"])
                if k in base and r.get("verdict") in ("accept", "reject"):
                    vrows.append({**base[k], "verdict": r["verdict"],
                                  "verify_note": str(r.get("verify_note") or "")[:400]})
            n = write_shard(Path(str(shard) + ".verified.jsonl"), vrows)
            n_verdicts += n
            print(f"skeptic: {shard.name}: {len(pending)} pending → {n} new verdicts")
        if not backlog:
            print("skeptic: no unverified ext_anchor rows — nothing to do")
        else:
            print(f"skeptic: {n_verdicts} verdicts total")

    print(f"done — {state['tokens']:,}/{budget:,} tokens, "
          f"{time.time() - t0:.0f}s, model={MODEL}"
          + ("  [ABORTED: window exhausted — rerun resumes]" if state["abort"] else ""))
    return 3 if state["abort"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--budget-tokens", type=int,
                    default=int(os.environ.get("WIKILEAN_BRAIN_AGENT_BUDGET",
                                               "500000")))
    ap.add_argument("--limit", type=int, default=200,
                    help="max candidate pairs per run (default 200)")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY,
                    help=f"agent calls in flight (clamped to {MAX_CONCURRENCY})")
    ap.add_argument("--roles", default="cartographer,skeptic",
                    help="comma-set of roles to run")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the work plan; no writes, no SDK")
    args = ap.parse_args()
    args.roles = {r.strip() for r in args.roles.split(",") if r.strip()}

    if args.dry_run:
        cands = gen_candidates(args.limit)
        backlog = skeptic_backlog()
        print(json.dumps({
            "dry_run": True, "model": MODEL,
            "budget_tokens": args.budget_tokens,
            "cartographer_candidates": len(cands),
            "candidate_sample": cands[:5],
            "rejected_cached": len(rejected_pairs()),
            "skeptic_pending": {f.name: len(v) for f, v in backlog.items()},
            "shard": str(SHARD.relative_to(REPO)),
        }, ensure_ascii=False, indent=2))
        return 0

    if _popped_key:
        print("(unset ANTHROPIC_API_KEY → Max-plan auth)")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
