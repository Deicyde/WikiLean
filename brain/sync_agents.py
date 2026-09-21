#!/usr/bin/env python3
"""Nightly Brain agent team (BRAIN v2 axis 4) — propose-only, budget-gated.

Three roles this version (docs/BRAIN-V2.md "Nightly brain sync"):

  cartographer  Candidate generation is DETERMINISTIC string matching: external
                pages with no CC0 qid (catalog/data/external/<db>_pages.jsonl)
                whose title exactly-or-nearly matches a Brain concept label.
                The agent only judges same-concept-or-not given the page
                title/snippet and the concept label/description. Accepted pairs
                → brain/proposals/ext_anchor_<date>.jsonl rows
                {"action":"xref","qid","xref":{"db","id"},"reason",...}.
  linker        Frontier-repo agent joins: walks a repo-link source's Lean
                modules (REPO_LINK_SOURCES, today tauceti) most-connective-
                first, --repo-modules per run, and proposes each module's
                PRIMARY mathematical objects as decl↔QID rows →
                brain/proposals/repo_link_<repo>_<date>.jsonl
                {"action":"repo_link","repo","decl","qid","kind":"mentions",…}.
                MENTIONS-ONLY by construction — the fold rejects any stronger
                kind (moderation contract: AI joins never mint identity
                claims). Judged modules (even zero-join ones) persist in
                brain/proposals/.repo_link_done_cache.jsonl.
  skeptic       Adversarial second opinion over ext_anchor AND repo_link rows
                that lack a verdict → <shard>.jsonl.verified.jsonl rows (the
                base row echoed + verdict/verify_note), the same overlay
                contract fold_proposals.py reads for every proposal family.

This script NEVER writes brain/data/ or catalog/data/ — fold_proposals.py
(deterministic verifier) is the only gate; its action:"xref" handler folds
verified rows into brain/data/ext_anchor_links.jsonl and its action:
"repo_link" handler into catalog/data/<repo>_links.jsonl for build_common.

Idempotent: (db, page-id, qid) pairs already present in ANY ext_anchor shard
are never re-proposed; pairs the cartographer judged NOT-same-concept persist
in brain/proposals/.ext_anchor_rejected_cache.jsonl and are never re-judged;
linker modules already judged persist in .repo_link_done_cache.jsonl; skeptic
rows already echoed in a .verified.jsonl are never re-judged. Runs to
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

from ingest.common import validate_external_directory

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

# ---- linker (repo_link) sources: registry key -> harvest jsonl --------------
# Every key must be a source_registry frontier_sources key — fold_proposals
# validates that again (its own registry read is the gate; this map only
# names the harvests the linker can walk).
REPO_LINK_SOURCES: dict[str, Path] = {
    "tauceti": REPO / "catalog" / "data" / "tauceti.jsonl",
}
# Modules already judged by the linker (rows {repo, module, judged_at, run,
# n_joins}) — zero-join modules count too, so the frontier always advances.
RL_DONE_CACHE = PROPOSALS / ".repo_link_done_cache.jsonl"
RL_CHUNK = 3        # modules per linker call
RL_DECLS_PER_MODULE = 12
RL_HINTS_PER_MODULE = 15
QID_RE = re.compile(r"^Q\d+$")

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
            # Current files use an object envelope; retain compatibility with
            # the original flat qid -> string/object map used by older snapshots.
            raw = raw.get("descriptions", raw) if isinstance(raw, dict) else raw
            if isinstance(raw, dict):
                for qid, value in raw.items():
                    if not isinstance(qid, str):
                        continue
                    if isinstance(value, str):
                        descriptions[qid] = value
                    elif isinstance(value, dict):
                        description = value.get("description")
                        descriptions[qid] = (
                            description if isinstance(description, str) else ""
                        )
        except json.JSONDecodeError:
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
    external_pairs = validate_external_directory(EXTERNAL)
    for db in sorted(external_pairs):
        _meta, page_rows, _links_meta, _links = external_pairs[db]
        for row in page_rows:
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


# ---------------------------------------------------------------------------
# Linker (repo_link) deterministic machinery
# ---------------------------------------------------------------------------

def _rl_row_key(r: dict) -> tuple:
    return (r.get("repo") or "", r.get("decl") or "", r.get("qid") or "")


def rl_done_modules(repo: str) -> set[str]:
    """Modules the linker already judged for `repo` (RL_DONE_CACHE rows)."""
    out: set[str] = set()
    if RL_DONE_CACHE.exists():
        for r in iter_jsonl(RL_DONE_CACHE):
            if r.get("repo") == repo and r.get("module"):
                out.add(r["module"])
    return out


def rl_record_done(repo: str, modules: list[str]) -> int:
    """Merge judged modules into RL_DONE_CACHE (first write wins, sorted,
    atomic tmp+rename). Returns modules newly added."""
    if not modules:
        return 0
    merged: dict[tuple[str, str], dict] = {}
    if RL_DONE_CACHE.exists():
        for r in iter_jsonl(RL_DONE_CACHE):
            merged.setdefault((r.get("repo") or "", r.get("module") or ""), r)
    judged_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for m in modules:
        k = (repo, m)
        if k not in merged:
            merged[k] = {"repo": repo, "module": m, "judged_at": judged_at,
                         "run": f"linker-{DATE}"}
            added += 1
    if not added:
        return 0
    RL_DONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RL_DONE_CACHE.with_suffix(RL_DONE_CACHE.suffix + ".tmp")
    with tmp.open("w") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k], ensure_ascii=False) + "\n")
    tmp.rename(RL_DONE_CACHE)
    return added


def _module_rank(module: str, rows: list[dict]) -> tuple:
    """Most-connective-first: area roots (Basic/Defs stems) over leaf lemma
    files, shallow paths over deep, bigger modules over smaller."""
    parts = module.split(".")
    return (0 if parts[-1] in ("Basic", "Defs", "Init") else 1,
            len(parts), -len(rows), module)


def _module_payload(repo: str, module: str, rows: list[dict],
                    concepts: dict[str, dict],
                    index: dict[str, list[str]]) -> dict:
    """One module's agent payload: its most define-y docstring'd decls plus
    candidate concept hints — known Brain concepts whose label appears as an
    n-gram of the module's docstring text (pre-verified label→QID pairs, so a
    hinted join can never carry a hallucinated QID)."""
    def drank(r: dict) -> tuple:
        return (0 if r.get("docstring") else 1,
                0 if r.get("kind") in ("def", "structure", "abbrev", "class",
                                       "inductive") else 1,
                r["decl"])
    decls = []
    for r in sorted(rows, key=drank)[:RL_DECLS_PER_MODULE]:
        d = {"decl": r["decl"], "kind": r.get("kind"),
             "docstring": (r.get("docstring") or "")[:280] or None}
        if not d["docstring"]:
            d["code"] = (r.get("code") or "")[:160] or None
        decls.append({k: v for k, v in d.items() if v})

    toks = norm(" ".join((r.get("docstring") or "") for r in rows)).split()
    hits: dict[str, str] = {}
    for n in range(1, 5):
        for i in range(len(toks) - n + 1):
            g = " ".join(toks[i:i + n])
            if n == 1 and len(g) < 5:
                continue        # single short words ("map", "set") spam hints
            for qid in index.get(g, ()):
                hits.setdefault(qid, concepts[qid]["label"])
    cands = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {"repo": repo, "module": module,
            "file": (rows[0].get("file") if rows else None),
            "candidates": [{"qid": q, "label": lb}
                           for q, lb in cands[:RL_HINTS_PER_MODULE]],
            "decls": decls}


def rl_gen_modules(repo: str, limit: int) -> list[dict]:
    """Ranked, not-yet-judged module payloads for one repo-link source."""
    path = REPO_LINK_SOURCES[repo]
    if not path.exists():
        print(f"NOTE: {path} missing — linker[{repo}] has no harvest, "
              f"0 modules", file=sys.stderr)
        return []
    mods: dict[str, list[dict]] = {}
    for r in iter_jsonl(path):
        if r.get("decl") and r.get("module"):
            mods.setdefault(r["module"], []).append(r)
    done = rl_done_modules(repo)
    todo = sorted((m for m in mods if m not in done),
                  key=lambda m: _module_rank(m, mods[m]))
    if limit is not None and limit >= 0:
        todo = todo[:limit]
    if not todo:
        return []
    concepts, index = load_concepts() if NODES.exists() else ({}, {})
    return [_module_payload(repo, m, mods[m], concepts, index) for m in todo]


_harvest_docs: dict[str, dict[str, str]] = {}


def harvest_docstring(repo: str, decl: str) -> str | None:
    """Docstring (or code header) of a harvested decl — skeptic context."""
    if repo not in _harvest_docs:
        d: dict[str, str] = {}
        p = REPO_LINK_SOURCES.get(repo)
        if p and p.exists():
            for r in iter_jsonl(p):
                if r.get("decl"):
                    d[r["decl"]] = (r.get("docstring") or r.get("code") or "")[:280]
        _harvest_docs[repo] = d
    return _harvest_docs[repo].get(decl or "") or None


def rl_backlog() -> dict[Path, list[dict]]:
    """repo_link shard -> base rows with no echoed verdict in its overlay."""
    out: dict[Path, list[dict]] = {}
    for f in sorted(PROPOSALS.glob("repo_link_*.jsonl")):
        if f.name.endswith(".verified.jsonl"):
            continue
        vf = Path(str(f) + ".verified.jsonl")
        echoed: set[tuple] = set()
        if vf.exists():
            for r in iter_jsonl(vf):
                echoed.add(_rl_row_key(r))
        pending = [r for r in iter_jsonl(f)
                   if r.get("repo") and r.get("decl") and r.get("qid")
                   and _rl_row_key(r) not in echoed]
        if pending:
            out[f] = pending
    return out


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

LINKER_SYSTEM = """\
You are the LINKER in the WikiLean Brain nightly sync. You receive Lean
modules from a frontier formalization repo (module name + a sample of its
declarations: name, kind, docstring/code header) plus, per module, candidate
concepts from the local index (label → QID pairs whose label literally occurs
in the module's docstrings — these QIDs are pre-verified).

For each module, identify its PRIMARY mathematical objects — the concepts the
module is fundamentally about (typically 0–4: the main definition/structure
and the central theorem's subject), NOT every lemma's every noun. For each
primary object, propose ONE join row anchored on the single listed declaration
that best embodies it (prefer the defining def/structure/abbrev/class; else
the central theorem).

Prefer the provided candidate QIDs. You may use a QID from your own knowledge
ONLY when you are certain of both the numeric id and its exact English
Wikidata label — a wrong or guessed QID poisons the graph; a missed join costs
nothing. When unsure, propose nothing for that module.

Every join is a 'mentions'-strength citation — NEVER a formalization or
identity claim (those need human review and are rejected by the fold).

OUTPUT — your final reply must be ONLY one JSON object, no prose. `module` and
`decl` must be byte-identical to the input:
{"rows": [{"module": "…", "decl": "…", "qid": "Q…",
           "qid_label": "<exact English Wikidata label>",
           "evidence": "<one sentence tying the declaration to the concept>",
           "confidence": "high"|"medium"}]}
Modules with no certain join simply contribute no rows.
"""

RL_SKEPTIC_SYSTEM = """\
You are the SKEPTIC in the WikiLean Brain nightly sync — the adversarial
second opinion on proposed frontier-repo declaration ↔ Wikidata-concept joins.
For each proposed row (repo, module, decl + its docstring, qid, qid_label,
the proposer's evidence), try to REFUTE it: is the QID actually a different
concept (homonym, broader field, narrower special case)? Is the claimed label
not the concept's English Wikidata label? Is the concept merely incidental to
the declaration rather than a primary object of its module? Accept only joins
you cannot refute.

OUTPUT — your final reply must be ONLY one JSON object, no prose. Echo every
input row exactly once, in the same order, repo/decl/qid byte-identical:
{"rows": [{"repo": "…", "decl": "…", "qid": "Q…",
           "verdict": "accept"|"reject", "verify_note": "<one sentence>"}]}
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


def _kf(r: dict, fields: tuple[str, ...]) -> tuple:
    """Filter key over `fields`, every value stringified (ids arrive as int
    or str depending on the source)."""
    return tuple(str(r.get(f)) for f in fields)


async def _judge(role_system: str, payloads: list[list[dict]], budget: int,
                 concurrency: int, state: dict,
                 key_fields: tuple[str, ...] = ("db", "id", "qid"),
                 ) -> list[dict]:
    """Run one role over its chunks (≤4 concurrent, budget-gated). Returns the
    agent rows, filtered to key_fields tuples actually dispatched — snippet/
    docstring text is a prompt-injection surface, so an agent can never mint a
    pair we did not ask about."""
    sem = asyncio.Semaphore(concurrency)
    out: list[dict] = []
    lock = asyncio.Lock()

    async def worker(chunk: list[dict]):
        async with sem:
            if state["abort"] or state["tokens"] >= budget:
                return
            allowed = {_kf(c, key_fields) for c in chunk}
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
                    if isinstance(r, dict) and _kf(r, key_fields) in allowed:
                        out.append(r)

    await asyncio.gather(*(worker(c) for c in payloads))
    return out


async def _linker_judge(payloads: list[list[dict]], budget: int,
                        concurrency: int, state: dict,
                        ) -> tuple[list[dict], list[str]]:
    """Run the linker over chunks of MODULE payloads. Returns (proposed rows
    filtered to (module, decl) pairs actually dispatched + a syntactic QID,
    modules whose call completed — only those enter the done cache, so a
    failed call's modules retry next run)."""
    sem = asyncio.Semaphore(concurrency)
    out: list[dict] = []
    done: list[str] = []
    lock = asyncio.Lock()

    async def worker(chunk: list[dict]):
        async with sem:
            if state["abort"] or state["tokens"] >= budget:
                return
            allowed = {(m["module"], d["decl"])
                       for m in chunk for d in m["decls"]}
            user = ("Lean modules with candidate concepts (JSON):\n"
                    + json.dumps(chunk, ensure_ascii=False)
                    + "\n\nPropose each module's primary-object joins per the "
                      "system prompt. Reply with ONLY the JSON object.")
            res = await _run_agent(LINKER_SYSTEM, user, state)
            if res is None or not isinstance(res.get("rows"), list):
                return
            async with lock:
                done.extend(m["module"] for m in chunk)
                for r in res["rows"]:
                    if (isinstance(r, dict)
                            and (r.get("module"), r.get("decl")) in allowed
                            and isinstance(r.get("qid"), str)
                            and QID_RE.match(r["qid"])):
                        out.append(r)

    await asyncio.gather(*(worker(c) for c in payloads))
    return out, done


# ---------------------------------------------------------------------------
# Atomic shard writes (merge + dedupe + sort — deterministic row order)
# ---------------------------------------------------------------------------

def _row_key(r: dict) -> tuple:
    x = r.get("xref") or {}
    return (x.get("db") or "", str(x.get("id") or ""), r.get("qid") or "")


def write_shard(path: Path, new_rows: list[dict], key=None) -> int:
    """Merge new rows into the shard, keyed by `key` (default: the ext-anchor
    (db,id,qid) key; repo_link shards pass _rl_row_key); first write wins
    (idempotent re-runs). Atomic tmp+rename. Returns rows added."""
    key = key or _row_key
    merged: dict[tuple, dict] = {}
    if path.exists():
        for r in iter_jsonl(path):
            merged[key(r)] = r
    added = 0
    for r in new_rows:
        k = key(r)
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

    # --- linker (frontier-repo agent joins; mentions-only proposals) ---------
    if "linker" in args.roles and not state["abort"]:
        for repo in args.repos:
            if repo not in REPO_LINK_SOURCES:
                print(f"linker[{repo}]: unknown repo-link source "
                      f"(known: {sorted(REPO_LINK_SOURCES)}) — skipped")
                continue
            if state["abort"] or state["tokens"] >= budget:
                print(f"linker[{repo}]: budget/window stop "
                      f"({state['tokens']:,}/{budget:,} tokens)")
                break
            payloads = rl_gen_modules(repo, args.repo_modules)
            if not payloads:
                print(f"linker[{repo}]: no undispatched modules — nothing to do")
                continue
            judged, done = await _linker_judge(
                _chunks(payloads, RL_CHUNK), budget, concurrency, state)
            mod_file = {m["module"]: m.get("file") for m in payloads}
            rows = []
            for r in sorted(judged, key=lambda r: (r["module"], r["decl"],
                                                   r["qid"])):
                conf = r.get("confidence")
                row = {
                    "action": "repo_link", "repo": repo,
                    "proposer": f"{proposer_tag}-linker-{DATE}",
                    "module": r["module"], "file": mod_file.get(r["module"]),
                    "decl": r["decl"], "qid": r["qid"],
                    "qid_label": str(r.get("qid_label") or "")[:200],
                    "kind": "mentions",   # the ONLY kind this channel mints
                    "confidence": conf if conf in ("high", "medium", "low")
                    else "medium",
                    "evidence": str(r.get("evidence") or "")[:400],
                }
                # fold would reject label-less/evidence-less rows anyway;
                # dropping them here keeps the shard clean
                if row["qid_label"].strip() and row["evidence"].strip():
                    rows.append(row)
            shard = PROPOSALS / f"repo_link_{repo}_{DATE}.jsonl"
            n_new = write_shard(shard, rows, key=_rl_row_key)
            n_done = rl_record_done(repo, done)
            print(f"linker[{repo}]: {len(payloads)} modules dispatched → "
                  f"{len(rows)} proposed joins → {n_new} new rows in "
                  f"{shard.name}; {n_done} modules newly cached as judged")
    elif "linker" in args.roles:
        print("linker: skipped (window exhausted)")

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

        # repo_link shards get the same adversarial pass (mentions fold
        # pending at capped medium — the skeptic verdict lifts the cap and
        # retracts refuted joins via the fold's any-reject veto)
        rl_bl = rl_backlog()
        n_rl_verdicts = 0
        for shard, pending in rl_bl.items():
            if state["abort"] or state["tokens"] >= budget:
                print(f"skeptic[repo_link]: budget/window stop before "
                      f"{shard.name} ({state['tokens']:,}/{budget:,} tokens)")
                break
            payload = [{"repo": r.get("repo"), "module": r.get("module"),
                        "decl": r.get("decl"),
                        "docstring": harvest_docstring(r.get("repo"),
                                                       r.get("decl")),
                        "qid": r.get("qid"), "qid_label": r.get("qid_label"),
                        "proposer_evidence": r.get("evidence")}
                       for r in pending]
            judged = await _judge(RL_SKEPTIC_SYSTEM, _chunks(payload, CHUNK),
                                  budget, concurrency, state,
                                  key_fields=("repo", "decl", "qid"))
            base = {_rl_row_key(r): r for r in pending}
            vrows = []
            for r in judged:
                k = (r.get("repo") or "", r.get("decl") or "",
                     r.get("qid") or "")
                if k in base and r.get("verdict") in ("accept", "reject"):
                    vrows.append({**base[k], "verdict": r["verdict"],
                                  "verify_note":
                                      str(r.get("verify_note") or "")[:400]})
            n = write_shard(Path(str(shard) + ".verified.jsonl"), vrows,
                            key=_rl_row_key)
            n_rl_verdicts += n
            print(f"skeptic[repo_link]: {shard.name}: {len(pending)} pending "
                  f"→ {n} new verdicts")
        if rl_bl:
            print(f"skeptic[repo_link]: {n_rl_verdicts} verdicts total")

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
    ap.add_argument("--roles", default="cartographer,skeptic,linker",
                    help="comma-set of roles to run")
    ap.add_argument("--repos", default="tauceti",
                    help="comma-set of repo-link sources the linker walks "
                         f"(known: {','.join(sorted(REPO_LINK_SOURCES))})")
    ap.add_argument("--repo-modules", type=int,
                    default=int(os.environ.get("WIKILEAN_BRAIN_REPO_MODULES",
                                               "8")),
                    help="max not-yet-judged modules the linker dispatches "
                         "per repo per run (default 8)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the work plan; no writes, no SDK")
    args = ap.parse_args()
    args.roles = {r.strip() for r in args.roles.split(",") if r.strip()}
    args.repos = [r.strip() for r in args.repos.split(",") if r.strip()]

    if args.dry_run:
        cands = gen_candidates(args.limit)
        backlog = skeptic_backlog()
        rl_bl = rl_backlog()
        linker_plan = {}
        for repo in args.repos:
            if repo not in REPO_LINK_SOURCES:
                linker_plan[repo] = "unknown repo-link source"
                continue
            payloads = rl_gen_modules(repo, args.repo_modules)
            linker_plan[repo] = {
                "modules_dispatched": len(payloads),
                "modules_done_cached": len(rl_done_modules(repo)),
                "module_sample": [{k: m[k] for k in
                                   ("module", "file", "candidates")}
                                  for m in payloads[:3]],
            }
        print(json.dumps({
            "dry_run": True, "model": MODEL,
            "budget_tokens": args.budget_tokens,
            "cartographer_candidates": len(cands),
            "candidate_sample": cands[:5],
            "rejected_cached": len(rejected_pairs()),
            "skeptic_pending": {f.name: len(v) for f, v in backlog.items()},
            "repo_link_skeptic_pending": {f.name: len(v)
                                          for f, v in rl_bl.items()},
            "linker": linker_plan,
            "shard": str(SHARD.relative_to(REPO)),
        }, ensure_ascii=False, indent=2))
        return 0

    if _popped_key:
        print("(unset ANTHROPIC_API_KEY → Max-plan auth)")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
