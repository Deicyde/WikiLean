"""In-process search tools for the moderation pipeline's Agent 2.

Wraps the reviewer search-skill CLIs (.claude/skills/*) as claude-agent-sdk
custom tools, so Agent 2 can verify Mathlib decls and Wikidata cross-references
as it annotates. Deliberately NOT raw Bash: Agent 2 consumes Wikipedia-derived
text (a prompt-injection surface), so it gets named, fixed-argv, read-only
search tools — the worst an injection can do is run a public search with an
attacker-chosen string, never a shell command.

Disable with WIKILEAN_SEARCH_TOOLS=0 (falls back to grep-only Agent 2).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from claude_agent_sdk import tool, create_sdk_mcp_server
    _SDK_OK = True
except Exception:  # SDK missing / too old — caller falls back to grep-only.
    _SDK_OK = False

SERVER_NAME = "wikilean"
_REPO = Path(__file__).resolve().parent.parent
_SKILLS = _REPO / ".claude" / "skills"
MATHLIB_CLI = _SKILLS / "mathlib-search" / "mathlib_search.py"
WIKIDATA_CLI = _SKILLS / "wikidata-search" / "wikidata.py"
BRAIN_CLI = _REPO / "brain" / "query.py"

_TIMEOUT = 45        # per search; the public APIs are usually < 2s
_MAX_OUT = 6000      # cap tool output so one search can't flood the agent context

# A key is a QID ONLY when it fully matches Q<digits> — a looser startswith("Q")
# would misroute Q-named decls (Quaternion, QuotientGroup.mk) away from the
# decl:Mathlib: fallback in _brain_unit_text.
_QID_RE = re.compile(r"^Q[0-9]+$")


def _run_cli(cli: Path, *argv: str, append_json: bool = True) -> str:
    """Run a skill CLI with fixed argv (no shell). Returns text for the agent.

    append_json=False for brain/query.py — JSON is its native output and it
    has no --json flag."""
    try:
        p = subprocess.run(
            [sys.executable, str(cli), *argv] + (["--json"] if append_json else []),
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"search timed out after {_TIMEOUT}s — treat as no result."
    except Exception as e:  # pragma: no cover - defensive
        return f"search failed to run: {e}"
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        err = (p.stderr or "").strip() or out or "(no output)"
        return f"no result / error (exit {p.returncode}): {err[:800]}"
    if not out:
        return "no result."
    return out[:_MAX_OUT] + ("\n…(truncated)" if len(out) > _MAX_OUT else "")


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _brain_unit_text(key: str) -> str:
    """brain_unit resolution: exact node lookup, then (for bare decl names —
    NOT QIDs, which are exactly Q<digits>) the decl:Mathlib: id form."""
    out = _run_cli(BRAIN_CLI, "node", key, append_json=False)
    if '"ok": false' in out and ":" not in key and " " not in key \
            and not _QID_RE.fullmatch(key):
        # bare decl name → the SCHEMA decl id form, then give up to search
        retry = _run_cli(BRAIN_CLI, "node", f"decl:Mathlib:{key}",
                         append_json=False)
        if '"ok": false' not in retry:
            out = retry
    try:
        entry = json.loads(out)
    except ValueError:
        return out
    # `unit` ships on concept payloads once the v2 core lands; degrade to
    # the full node entry (payload + edges) when absent.
    unit = (entry.get("node") or {}).get("unit") if isinstance(entry, dict) else None
    if unit:
        return json.dumps({"ok": True, "id": (entry.get("node") or {}).get("id") or key,
                           "unit": unit}, ensure_ascii=False)
    return out


def build_search_server():
    """Return (server, tool_names) for Agent 2, or (None, []) if disabled.

    Cheap and network-free: only builds the in-process server object.
    """
    if not _SDK_OK or os.environ.get("WIKILEAN_SEARCH_TOOLS") == "0":
        return None, []
    if not MATHLIB_CLI.exists() or not WIKIDATA_CLI.exists():
        return None, []

    @tool(
        "decl_exists",
        "Confirm a Mathlib4 declaration name EXISTS and get its module. Use this "
        "before citing any decl as a formalization — never output a `decl` you "
        "have not confirmed (a hallucinated name is worse than not_formalized). "
        "Input: the exact dotted name, e.g. 'Nat.add_comm'.",
        {"name": str},
    )
    async def decl_exists(args):
        return _text(await asyncio.to_thread(_run_cli, MATHLIB_CLI, "decl", args["name"]))

    @tool(
        "loogle",
        "Find a Mathlib4 lemma by pattern when you don't know the exact name. "
        "Query forms (comma = AND): a dotted constant `Real.sin`; a quoted "
        "name-substring `\"add_comm\"`; a type pattern with ?a/?b metavariables "
        "`?a * ?b = ?b * ?a`; or a main conclusion prefixed with `|-`. Anchor "
        "equation/inequality patterns under `|-` or with a constant or it may "
        "time out.",
        {"query": str},
    )
    async def loogle(args):
        return _text(await asyncio.to_thread(_run_cli, MATHLIB_CLI, "loogle", args["query"]))

    @tool(
        "mathlib_semantic",
        "Natural-language search of Mathlib4 — describe the statement in prose "
        "and get candidate decls with informal descriptions. Use when grep, "
        "decl_exists, and loogle have not settled the match.",
        {"query": str},
    )
    async def mathlib_semantic(args):
        return _text(await asyncio.to_thread(_run_cli, MATHLIB_CLI, "semantic", args["query"]))

    @tool(
        "wikidata_search",
        "Find the Wikidata QID(s) for a concept name. Returns candidates with "
        "descriptions so you can disambiguate (do not trust the top hit blindly). "
        "Label/alias PREFIX match only — for phrasing that may not match the "
        "Wikidata label, prefer wikidata_semantic.",
        {"label": str},
    )
    async def wikidata_search(args):
        return _text(await asyncio.to_thread(_run_cli, WIKIDATA_CLI, "search", args["label"]))

    @tool(
        "wikidata_by_slug",
        "Resolve an English-Wikipedia article title/slug to its Wikidata QID by "
        "EXACT sitelink lookup (not a search). Use this for the article you are "
        "annotating — its own QID is an exact anchor, no guessing. Input: the "
        "enwiki title or slug, e.g. 'Determinant' or 'Pythagorean_theorem'.",
        {"title": str},
    )
    async def wikidata_by_slug(args):
        return _text(await asyncio.to_thread(_run_cli, WIKIDATA_CLI, "by_slug", args["title"]))

    @tool(
        "wikidata_semantic",
        "Find the best-matching Wikidata QID(s) for a concept by MEANING, not "
        "just label spelling. Describe the concept in prose; returns candidates "
        "ranked by semantic similarity with descriptions to disambiguate. Prefer "
        "this over wikidata_search when the concept's phrasing may not match its "
        "Wikidata label, then confirm the chosen QID with wikidata_xrefs.",
        {"description": str},
    )
    async def wikidata_semantic(args):
        return _text(await asyncio.to_thread(_run_cli, WIKIDATA_CLI, "semantic", args["description"]))

    @tool(
        "wikidata_xrefs",
        "Given a Wikidata QID, list its formal-library cross-references (Metamath, "
        "nLab, MathWorld, ProofWiki, defining formula) and its English Wikipedia "
        "article. Optional context for judging whether a concept is formalized "
        "elsewhere. Input: a QID like 'Q11518'.",
        {"qid": str},
    )
    async def wikidata_xrefs(args):
        return _text(await asyncio.to_thread(_run_cli, WIKIDATA_CLI, "xrefs", args["qid"]))

    # --- BRAIN tools (read-only, shard-backed; wraps brain/query.py, whose
    # native output is JSON — no --json flag). Degrade to the 7 search tools
    # when the CLI or its shards are absent (fresh clone before a brain build).
    # Key resolution lives in module-level _brain_unit_text (self-testable).
    @tool(
        "brain_node",
        "Look up one WikiLean Brain node by EXACT id: a QID ('Q181296'), a "
        "container ('path:Mathlib/CategoryTheory'), a decl "
        "('decl:Mathlib:CommGroup'), or an external page "
        "('xref:nlab:abelian+group'). Returns the node payload plus its typed "
        "1-hop edges (formalizes/xref/depends/relates/…) with provenance, the "
        "containment breadcrumb, and a children summary. Use brain_search "
        "first when you only have a name.",
        {"id": str},
    )
    async def brain_node(args):
        return _text(await asyncio.to_thread(
            _run_cli, BRAIN_CLI, "node", args["id"], append_json=False))

    @tool(
        "brain_search",
        "Search Brain concepts/containers/decls by label substring; returns "
        "node ids with type/label/status. The way to find a node id for "
        "brain_node/brain_unit when you only have a concept name.",
        {"q": str},
    )
    async def brain_search(args):
        return _text(await asyncio.to_thread(
            _run_cli, BRAIN_CLI, "search", args["q"], append_json=False))

    @tool(
        "brain_unit",
        "Resolve a key — QID ('Q181296'), decl id ('decl:Mathlib:CommGroup'), "
        "or bare decl name ('CommGroup') — to its atomic unit card: the "
        "article ∘ QID ∘ Mathlib decls ∘ external cross-refs identity the "
        "Brain has verified for that concept. Falls back to the full node "
        "entry when no unit card is stored.",
        {"key": str},
    )
    async def brain_unit(args):
        return _text(await asyncio.to_thread(_brain_unit_text, args["key"]))

    tools = [decl_exists, loogle, mathlib_semantic,
             wikidata_search, wikidata_by_slug, wikidata_semantic, wikidata_xrefs]
    if BRAIN_CLI.exists():
        tools += [brain_node, brain_search, brain_unit]
    server = create_sdk_mcp_server(SERVER_NAME, "1.0.0", tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names


# Guidance appended to AGENT2_SYSTEM when the tools are wired in.
AGENT2_TOOLS_GUIDANCE = """

VERIFICATION TOOLS — use them, do not rely on grep alone:
- decl_exists(name): confirm a Mathlib decl is REAL before citing it. Never
  output a `decl` you have not confirmed by grep/read OR decl_exists. A
  hallucinated decl is worse than "not_formalized".
- loogle(query): find a lemma by pattern when grep is unfruitful (dotted
  constant, "name substring", ?a/?b type pattern, or `|-` conclusion).
- mathlib_semantic(query): natural-language fallback for uncertain matches.
- wikidata_by_slug(title): the article you are annotating maps to its QID by
  EXACT enwiki sitelink — use this for the top-level concept, never guess it.
- wikidata_semantic(description): meaning-based Wikidata search over the curated
  math-QID universe. PREFER it over wikidata_search(label) when the concept may
  be phrased differently from its Wikidata label (label search is prefix-only
  and grabs plausible-but-too-broad QIDs). Then confirm the chosen QID with
  wikidata_xrefs(qid).
- wikidata_search(label) / wikidata_xrefs(qid): optional — label-prefix search
  and a check of what formal references a concept already carries.
- brain_search(q) / brain_node(id) / brain_unit(key): the BRAIN — WikiLean's
  verified concept↔Mathlib graph. brain_search finds a node id by label;
  brain_node returns its typed neighborhood (formalizes/xref/… edges with
  provenance); brain_unit resolves a QID, decl:Mathlib:<name>, or bare decl
  name to the atomic unit card (article ∘ QID ∘ decls ∘ cross-refs). Check
  what the graph already believes formalizes a concept before classifying.
These are cheap and prevent wrong citations; prefer them to settle any match
you are not certain of.
"""


if __name__ == "__main__":
    # Zero-agent smoke: prove each wrapped CLI returns real data.
    # QID-routing invariant first (pure, no subprocess): only Q<digits> is a
    # QID; Q-named decls must stay eligible for the decl:Mathlib: fallback.
    assert _QID_RE.fullmatch("Q181296"), "Q181296 must parse as a QID"
    for k in ("Quaternion", "QuotientGroup.mk", "Q", "Q12x"):
        assert not _QID_RE.fullmatch(k), f"{k!r} must NOT parse as a QID"
    print("QID-routing asserts OK (Q181296 is a QID; Quaternion/QuotientGroup.mk are not)")
    server, names = build_search_server()
    print("server built:", server is not None, "| tools:", names)
    for cli, argv, append_json in [
        (MATHLIB_CLI, ("decl", "Nat.add_comm"), True),
        (MATHLIB_CLI, ("loogle", "Real.sin, Continuous"), True),
        (WIKIDATA_CLI, ("xrefs", "Q11518"), True),
        (BRAIN_CLI, ("node", "Q181296"), False),
        (BRAIN_CLI, ("search", "abelian"), False),
    ]:
        print(f"\n$ {cli.name} {' '.join(argv)}" + (" --json" if append_json else ""))
        print(_run_cli(cli, *argv, append_json=append_json)[:300])
    if BRAIN_CLI.exists():
        # Q-named bare decl: must take the decl:Mathlib: fallback (or miss
        # cleanly), never be swallowed by a QID parse.
        print("\n$ brain_unit('Quaternion')  (Q-named decl → decl fallback, not QID)")
        print(_brain_unit_text("Quaternion")[:300])
