#!/usr/bin/env python3
"""Batch annotation orchestrator for WikiLean.

Runs the full annotation pipeline across many articles:

    fetch → extract → Agent 1 (enumerate) → validate → Agent 2 (Mathlib) → render

The two agents run via claude-agent-sdk (Max-plan auth — pop ANTHROPIC_API_KEY
before importing the SDK). Deterministic steps shell out to the tested scripts
(extract_sections.py, validate_coverage.py, render.py). Article-level
concurrency; resumable — an article whose out/<slug>.html already exists is
skipped.

Run with the venv that has claude-agent-sdk:
    catalog/.venv/bin/python site/batch_annotate.py --limit 3            # smoke
    catalog/.venv/bin/python site/batch_annotate.py --concurrency 6      # full
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# Pop the API key BEFORE importing the SDK so the spawned `claude` subprocess
# uses the Max-subscription login rather than billing an API account.
# moderate.py --auth api-key sets WIKILEAN_KEEP_API_KEY=1 (before importing this
# module) to leave the key in place so the SDK bills the API account instead.
_popped_key = None
if os.environ.get("WIKILEAN_KEEP_API_KEY") != "1":
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
CACHE = HERE / "cache"
ANNOT = HERE / "annotations"
OUT = HERE / "out"
CATALOG_DATA = HERE.parent / "catalog" / "data"
RUN_LOG = CACHE / ".batch_run.log"

MATHLIB = Path(os.environ.get("WIKILEAN_MATHLIB", "/Users/jack/Desktop/LEAN/mathlib4"))
MODEL = "claude-opus-4-7"  # pinned agent model; recorded in run meta by moderate.py
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_UA = "WikiLean/0.1 (https://github.com/Deicyde/WikiLean; jack.mccarthy.1@stonybrook.edu)"
PY = sys.executable if "venv" not in sys.executable else "python3"


# ---------------------------------------------------------------------------
# Agent system prompts (refined from the manual smoke-test runs)
# ---------------------------------------------------------------------------

AGENT1_SYSTEM = """\
You are Agent 1 in the WikiLean annotation pipeline. Given the extracted
plain-text sections of a Wikipedia mathematics article, enumerate every
distinct mathematical statement (definition, proposition/theorem, example).

RULES:
1. Every `snippet` MUST be copied CHARACTER-FOR-CHARACTER from the paragraph
   text given to you. Do NOT paraphrase or fix whitespace. Never include
   `[MATH]` in a snippet — pick words from the non-math portions only.
2. Theorem boxes (paragraphs starting `[THEOREM BOX: "..."]`): use anchor
   `{"type": "theorem_box", "value": "<the label>"}` to highlight the whole box.
3. Multi-paragraph statements already merged into one paragraph (e.g.
   "X is Y if: cond1, cond2"): ONE annotation; snippet from the intro portion.
4. Each annotation covers exactly ONE statement. Pick a snippet from WITHIN
   the target sentence — the renderer expands it to sentence boundaries.
5. Do NOT skip statements. Cover every definition, proposition, theorem, example.

OUTPUT — your final reply must be ONLY one JSON object, no prose:
{"annotations": [
  {"kind": "definition"|"proposition"|"theorem"|"example",
   "label": "<short human-readable name>",
   "anchor": {"section": "<exact heading>", "snippet": "<verbatim phrase>"}}
]}
For a theorem box use "anchor": {"type": "theorem_box", "value": "<label>"} instead.
"""

AGENT2_SYSTEM = """\
You are Agent 2 in the WikiLean annotation pipeline. Given a list of
mathematical statements from a Wikipedia article, determine for each whether
it is formalized in Mathlib4. Mathlib4 is the current working directory; only
look in `Mathlib/`.

For EACH statement:
  1. Grep/Read Mathlib to find a formalizing declaration.
  2. Classify: "formalized" (direct match), "partial" (related infra exists but
     not the exact statement), or "not_formalized".
  3. Record the Mathlib decl name, dotted module path, and match_kind
     ("exact" | "generalization" | "special_case" | "invocation" | null).
  4. Write a one-sentence note. Only cite decls you verified by grep/read.

OUTPUT — your final reply must be ONLY one JSON object, no prose. Echo back
every input annotation in the SAME ORDER, preserving kind/label/anchor AND
`id` AND `provenance` exactly (echo an input annotation's `id` string back
unchanged and never invent one; if an input annotation has provenance "human"
or "ai-moderated", echo that string back unchanged — do NOT downgrade it to
"ai"), adding status/mathlib/note:
{"annotations": [
  {"kind": "...", "label": "...", "anchor": {...},
   "status": "formalized"|"partial"|"not_formalized",
   "mathlib": {"decl": <str|null>, "module": <str|null>, "match_kind": <str|null>},
   "note": "<one sentence>",
   "provenance": "<echoed verbatim from input, if present>"}
]}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_slug(title: str) -> str:
    """Filesystem-safe slug. 'Picard–Lindelöf theorem' → 'Picard-Lindelof_theorem'."""
    s = title.replace("–", "-").replace("—", "-")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_.\-]", "", s)
    return s


def parse_json_object(text: str) -> dict | None:
    """Extract the first balanced {...} JSON object from text."""
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
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def fetch_html(slug: str, title: str, target_revid: int | None = None) -> bool:
    """Fetch + cache the article HTML. Returns True on success.

    With `target_revid` (fix F1), fetches THAT exact revision via
    action=parse&oldid= into the revid-suffixed cache —
    cache/<slug>.<revid>.html + .meta.json, render.py's pinned-cache
    convention — and NEVER trusts the un-suffixed legacy cache, which can be
    any age while D1 pins a specific revision."""
    suffix = f".{target_revid}" if target_revid is not None else ""
    path = CACHE / f"{slug}{suffix}.html"
    if path.exists() and path.stat().st_size > 0:
        return True
    if target_revid is None:
        params = {
            "action": "parse", "page": title, "prop": "text|revid",
            "format": "json", "formatversion": "2", "redirects": "1",
        }
    else:
        # oldid= addresses a single immutable revision; page/redirects don't apply.
        params = {
            "action": "parse", "oldid": str(target_revid), "prop": "text|revid",
            "format": "json", "formatversion": "2",
        }
    try:
        r = requests.get(WIKI_API, params=params,
                         headers={"User-Agent": WIKI_UA}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "parse" not in data:
            return False
        path.write_text(data["parse"]["text"], encoding="utf-8")
        revid = data["parse"].get("revid")
        if revid:
            import datetime as _dt
            meta = {
                "slug": slug, "wikipedia_title": title, "revid": revid,
                "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "pinned_via": "fetch" if target_revid is None else "oldid",
            }
            (CACHE / f"{slug}{suffix}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def run_script(script: str, slug: str) -> tuple[int, str]:
    """Run a deterministic pipeline script for one slug. Returns (rc, output)."""
    proc = subprocess.run(
        [PY, str(HERE / script), slug],
        capture_output=True, text=True, cwd=str(HERE),
    )
    return proc.returncode, (proc.stdout + proc.stderr)


async def run_agent(system: str, user: str, cwd: Path,
                    tools: list[str], max_turns: int,
                    mcp_servers: dict | None = None) -> tuple[dict | None, dict]:
    """Run one agent via the SDK. Returns (parsed_json_or_None, meta)."""
    opt_kwargs = dict(
        model=MODEL,
        system_prompt=system,
        allowed_tools=tools,
        cwd=str(cwd),
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )
    if mcp_servers:
        opt_kwargs["mcp_servers"] = mcp_servers
    options = ClaudeAgentOptions(**opt_kwargs)
    last_text = ""
    result_obj = None
    n_tool = 0
    tools_used: dict[str, int] = {}
    try:
        async for msg in query(prompt=user, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        last_text = b.text or last_text
                    elif isinstance(b, ToolUseBlock):
                        n_tool += 1
                        name = getattr(b, "name", "?")
                        tools_used[name] = tools_used.get(name, 0) + 1
            elif isinstance(msg, ResultMessage):
                result_obj = msg
                if msg.result:
                    last_text = msg.result
    except Exception as e:
        # The SDK raises a generic "Claude Code returned an error result:
        # <subtype>" and discards the CLI's actual result text — which is where
        # the real cause lives (e.g. "Credit balance is too low" when an
        # ANTHROPIC_API_KEY shadows the Max login, or a rate-limit notice). The
        # ResultMessage is yielded just before the raise, so surface its text so
        # the runner logs WHY instead of the opaque subtype. Keeps "limit"/
        # "credit balance" substrings the runner keys on for fast abort.
        detail = getattr(result_obj, "result", None) if result_obj else None
        raise RuntimeError(f"agent_error: {detail or e}") from e
    usage = getattr(result_obj, "usage", None) if result_obj else None
    tokens = 0
    if isinstance(usage, dict):
        tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    meta = {
        "n_tool_calls": n_tool,
        "tools_used": tools_used,
        # NOTE: this is the *equivalent* API cost the SDK reports; under
        # Max-plan auth no per-token dollars are billed — it's a usage proxy.
        "cost_usd_equiv": getattr(result_obj, "total_cost_usd", None) if result_obj else None,
        "tokens": tokens,
        "duration_ms": getattr(result_obj, "duration_ms", None) if result_obj else None,
    }
    return parse_json_object(last_text), meta


# Search tools for Agent 2 (decl/QID verification). Built once; (None, []) when
# disabled via WIKILEAN_SEARCH_TOOLS=0 or if the skill CLIs/SDK are unavailable.
try:
    from search_tools import build_search_server, AGENT2_TOOLS_GUIDANCE
    _SEARCH_SERVER, _SEARCH_TOOLS = build_search_server()
except Exception:
    _SEARCH_SERVER, _SEARCH_TOOLS, AGENT2_TOOLS_GUIDANCE = None, [], ""

# Step 2 (propose-then-approve): when WIKILEAN_PROPOSALS=1, Agent 2 (moderation
# mode) may attach a SEARCH-VERIFIED `moderation_proposal` to a human annotation it
# believes is wrong. `_preserve_human` harvests these into the ladder → meta.ladder
# .proposals → moderation_state.proposal, where the human approves/rejects on the
# site. Gated because the added prompt text bumps prompt_sha (research comparability).
_PROPOSALS = os.environ.get("WIKILEAN_PROPOSALS") == "1"
AGENT2_PROPOSAL_GUIDANCE = """

PROPOSING corrections to human annotations (moderation mode only):
A `provenance:"human"` annotation is authoritative — echo its status and mathlib
UNCHANGED. But when your Mathlib search VERIFIES its mapping is now wrong — it says
`not_formalized` yet you confirmed a real decl, or it cites a decl your search shows
does not exist — attach a `moderation_proposal` to that annotation (a suggestion for
a human to approve; do NOT change the annotation itself):
  "moderation_proposal": {"fields": {"status": "…", "mathlib": {"decl": "…",
  "module": "…", "match_kind": "…"}}, "reason": "one sentence citing the verified decl"}
Rules: propose ONLY fields you search-verified THIS pass; omit `moderation_proposal`
entirely if unsure; never attach it to a non-human annotation (fix those directly);
leave the human annotation's own status/mathlib exactly as given."""


# ---------------------------------------------------------------------------
# Per-article pipeline
# ---------------------------------------------------------------------------

MODERATE_AGENT1_SYSTEM = """\
You are the MODERATION agent for WikiLean, reviewing one article's EXISTING
annotations against its text. You receive the article's extracted sections AND
its current annotations, each tagged with `provenance` ("human" or "ai…").

Produce the full, improved annotation set:
1. PRESERVE every annotation with provenance "human" — keep its anchor, label,
   kind, status, mathlib, and note exactly. NEVER delete or rewrite a human
   annotation. If you think one is wrong, keep it unchanged and add a short
   "moderation_flag" field explaining the concern.
2. Review the AI annotations: fix clearly-wrong kind/label/anchor, drop exact
   duplicates, otherwise keep them. Mark any you change provenance "ai-moderated".
3. ADD annotations for mathematical statements not yet covered (improve
   coverage); mark new ones provenance "ai".
4. Copy each `snippet`/anchor value CHARACTER-FOR-CHARACTER; never include `[MATH]`.
5. An annotation with status "rejected" is a human veto (tombstone) — never
   delete it, never change it, and never re-annotate that statement.
6. Echo each annotation's `id` verbatim when present; never invent ids (new
   annotations are assigned ids downstream — leave them without one).

OUTPUT — ONLY one JSON object, carrying each annotation's provenance:
{"annotations": [{"kind":"…","label":"…","anchor":{…},"status":"…",
"mathlib":{…},"note":"…","provenance":"…"}]}
"""


def _anchor_sig(a: dict) -> str:
    """Stable signature of an annotation's anchor, for matching across passes.

    LOCKSTEP CONTRACT (F12 — identical in moderate._anchor_sig and
    wiki/src/validation.ts anchorSig): a plain-dict `anchor` is used as before;
    a non-dict `anchor` (string/list/number — must not crash) falls through to
    `anchors[0]` when `anchors` is a non-empty list whose first element is a
    plain dict; otherwise the all-null signature."""
    anc = a.get("anchor")
    if not isinstance(anc, dict):
        anchors = a.get("anchors")
        if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict):
            anc = anchors[0]
        else:
            anc = {}
    return json.dumps([anc.get("type"), anc.get("section"), anc.get("snippet"),
                       anc.get("value"), anc.get("from")], sort_keys=True)


def _preserve_human(existing: list[dict], produced: list[dict],
                    ) -> tuple[list[dict], dict]:
    """Deterministic guarantee that human contributions survive a moderation
    pass: any human annotation the agent altered is restored to its original
    form, and any it dropped is re-inserted. The agent's smart work stands for
    everything else. Returns (annotations, ladder stats) — `restored` counts
    only annotations the agent actually altered; `reinserted` counts drops.

    Matching is by id FIRST when both sides carry string ids, anchor-sig
    fallback (fix F7: an agent that re-anchors a human annotation gets its
    altered copy REPLACED in place, not duplicated by an append). Any
    `moderation_flag` string the agent attached to its copy is harvested into
    stats['flags'] as [id, flag] pairs before the restore strips it (fix F14:
    the agent's dissent survives even though its edit doesn't)."""
    stats: dict = {"restored": 0, "reinserted": 0, "flags": []}
    human = [a for a in existing if a.get("provenance") == "human"]
    if not human:
        return produced, stats
    by_id = {a["id"]: i for i, a in enumerate(produced)
             if isinstance(a.get("id"), str) and a["id"]}
    by_sig = {_anchor_sig(a): i for i, a in enumerate(produced)}
    out = list(produced)
    consumed: set[int] = set()
    for h in human:
        hid = h.get("id")
        idx = by_id.get(hid) if isinstance(hid, str) and hid else None
        if idx is None or idx in consumed:
            idx = by_sig.get(_anchor_sig(h))
        if idx is not None and idx not in consumed:
            consumed.add(idx)
            flag = out[idx].get("moderation_flag")            # F14: harvest dissent
            if isinstance(flag, str) and flag:
                stats["flags"].append([hid if isinstance(hid, str) else None, flag])
            # Step 2: harvest a search-verified proposed change to this human
            # annotation. Needs a live string id (the store validates ids) and a
            # non-empty fields dict; the restore below strips moderation_proposal.
            prop = out[idx].get("moderation_proposal")
            if (isinstance(hid, str) and hid and isinstance(prop, dict)
                    and isinstance(prop.get("fields"), dict) and prop["fields"]):
                # setdefault (not a fixed key): a proposal-free pass keeps the
                # historical stats shape {restored,reinserted,flags} — the
                # cross-language parity fixture + goldens assert it byte-for-byte.
                stats.setdefault("proposals", []).append({
                    "annotationId": hid, "fields": prop["fields"],
                    "reason": prop["reason"] if isinstance(prop.get("reason"), str) else "",
                })
            restored = {**h, "provenance": "human"}           # restore original
            if out[idx] != restored:
                stats["restored"] += 1
            out[idx] = restored
        else:
            out.append({**h, "provenance": "human",
                        "moderation_note": "re-inserted by moderator (agent omitted it)"})
            stats["reinserted"] += 1
    # Defensive: `moderation_proposal` is a transient harvest field — never let it
    # ride into a stored annotation (a matched-human copy is already replaced by
    # the clean restore above; this strips any on an unmatched/AI annotation).
    for a in out:
        if isinstance(a, dict) and "moderation_proposal" in a:
            a.pop("moderation_proposal", None)
    return out, stats


def _merge_proposals(ladder: dict, proposals: list) -> None:
    """Add step-2 proposals to the ladder, deduped — but create the `proposals`
    key ONLY when there is something to add, so a proposal-free / gated-off pass
    keeps the historical ladder shape (the eval + build_meta goldens assert it)."""
    if not proposals:
        return
    lp = ladder.setdefault("proposals", [])
    lp += [p for p in proposals if p not in lp]


async def annotate_one(article: dict, sem: asyncio.Semaphore, seed_decls: dict,
                       moderate: bool = False,
                       existing_override: list[dict] | None = None,
                       target_revid: int | None = None) -> dict:
    title = article["title"]
    slug = make_slug(title)
    rec = {"title": title, "slug": slug}
    if target_revid is not None:
        rec["target_revid"] = target_revid
    # F1: with a pinned target_revid every per-slug artifact (HTML, sections)
    # comes from the revid-suffixed cache; the legacy un-suffixed cache is
    # never consulted, so the agents review exactly the revision D1 pins.
    cache_slug = f"{slug}.{target_revid}" if target_revid is not None else slug
    t0 = time.time()
    async with sem:
        try:
            # 1. fetch (at the pinned revid when given — F1)
            if not fetch_html(slug, title, target_revid=target_revid):
                rec["error"] = "fetch_failed"
                return rec
            # 2. extract — runs on the revid-pinned HTML when given (F1)
            rc, _ = run_script("extract_sections.py", cache_slug)
            if rc != 0:
                rec["error"] = "extract_failed"
                return rec
            sections = json.loads((CACHE / f"{cache_slug}.sections.json").read_text())

            # Moderation mode: load the current annotations so the agents are
            # context-aware and human edits are preserved (not clobbered).
            # moderate.py passes existing_override sourced from the live D1 API
            # (GET /api/article/:slug.json) — D1 is canonical; skip the disk read.
            existing: list[dict] = []
            if moderate:
                if existing_override is not None:
                    existing = existing_override
                else:
                    ap = ANNOT / f"{slug}.json"
                    if ap.exists():
                        try:
                            existing = json.loads(ap.read_text()).get("annotations", [])
                        except (json.JSONDecodeError, OSError):
                            existing = []
            do_moderate = moderate and bool(existing)
            rec["mode"] = "moderate" if do_moderate else "regen"

            # 3. Agent 1 — enumerate, or moderate the existing set
            if do_moderate:
                a1_prompt = (
                    f"Article: {title}\n\nExtracted sections (JSON):\n"
                    f"{json.dumps(sections['sections'], ensure_ascii=False)}\n\n"
                    f"CURRENT annotations (JSON):\n"
                    f"{json.dumps(existing, ensure_ascii=False)}\n\n"
                    "Moderate per the system prompt. Reply with ONLY the JSON object."
                )
                a1_system = MODERATE_AGENT1_SYSTEM
            else:
                a1_prompt = (
                    f"Article: {title}\n\nExtracted sections (JSON):\n"
                    f"{json.dumps(sections['sections'], ensure_ascii=False)}\n\n"
                    "Enumerate every mathematical statement per the system prompt. "
                    "Reply with ONLY the JSON object."
                )
                a1_system = AGENT1_SYSTEM
            a1, a1_meta = await run_agent(a1_system, a1_prompt, HERE, [], 12)
            if not a1 or "annotations" not in a1:
                rec["error"] = "agent1_no_json"
                return rec
            annotations = a1["annotations"]
            ladder = {"restored": 0, "reinserted": 0, "downgrades_blocked": 0,
                      "moderation_flags": []}
            if do_moderate:
                annotations, ph1 = _preserve_human(existing, annotations)
                ladder["restored"] += ph1["restored"]
                ladder["reinserted"] += ph1["reinserted"]
                # F14: agent dissent on human annotations survives in the ladder.
                ladder["moderation_flags"] += [f for f in ph1["flags"]
                                               if f not in ladder["moderation_flags"]]
                # Step 2: proposals ride the ladder ONLY when found — a gated-off /
                # proposal-free pass keeps the historical ladder shape byte-for-byte.
                _merge_proposals(ladder, ph1.get("proposals"))
            envelope = {
                "slug": slug, "wikipedia_title": title, "display_title": title,
                "schema_version": 3, "annotations": [
                    {**a, "provenance": "ai-agent1"} for a in annotations
                ],
            }
            (ANNOT / f"{slug}.agent1.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

            # 4. validate coverage (advisory)
            _, cov_out = run_script("validate_coverage.py", slug)
            m = re.search(r"Coverage: \d+/\d+ \w+ \w+ \((\d+)%\)", cov_out)
            rec["coverage_pct"] = int(m.group(1)) if m else None

            # 5. Agent 2 — Mathlib matching (greps mathlib4)
            seed = seed_decls.get(title)
            seed_hint = ""
            if seed:
                seed_hint = ("\n\nPrior pass found these Mathlib decls for the "
                             f"article's central concept (use as leads, verify): {seed}")
            a2_prompt = (
                f"Article: {title}\n\nStatements to classify (JSON):\n"
                f"{json.dumps(annotations, ensure_ascii=False)}{seed_hint}\n\n"
                "Classify each against Mathlib4 per the system prompt. "
                "Reply with ONLY the JSON object."
            )
            a2_system = (AGENT2_SYSTEM + (AGENT2_TOOLS_GUIDANCE if _SEARCH_TOOLS else "")
                         + (AGENT2_PROPOSAL_GUIDANCE if (_PROPOSALS and do_moderate) else ""))
            a2, a2_meta = await run_agent(
                a2_system, a2_prompt, MATHLIB,
                ["Read", "Grep", "Glob"] + _SEARCH_TOOLS, 60,
                mcp_servers={"wikilean": _SEARCH_SERVER} if _SEARCH_SERVER else None,
            )
            if not a2 or "annotations" not in a2:
                rec["error"] = "agent2_no_json"
                return rec
            a2_annos = a2["annotations"]
            if do_moderate:
                # Restore human annotations' decls/status after Agent 2 too, so
                # the matcher can't override a human-verified mapping.
                a2_annos, ph2 = _preserve_human(existing, a2_annos)
                ladder["restored"] += ph2["restored"]
                ladder["reinserted"] += ph2["reinserted"]
                ladder["moderation_flags"] += [f for f in ph2["flags"]
                                               if f not in ladder["moderation_flags"]]
                _merge_proposals(ladder, ph2.get("proposals"))
                # Deterministic provenance carry-through. Agent 2 must not be
                # able to DOWNGRADE Agent 1's review marks: if the pre-Agent-2
                # state had "ai-moderated" or "human" for this anchor, that
                # wins regardless of what Agent 2 echoed back.
                PRIORITY = {"human": 3, "ai-moderated": 2, "ai": 1,
                            "ai-agent1": 1, None: 0}
                pre_a2_prov = {_anchor_sig(a): a.get("provenance") for a in annotations}
                out_annos = []
                for a in a2_annos:
                    if a.get("provenance") == "human":
                        out_annos.append(a)  # _preserve_human already restored
                        continue
                    inherited = pre_a2_prov.get(_anchor_sig(a))
                    echoed = a.get("provenance")
                    winner = max((inherited, echoed), key=lambda p: PRIORITY.get(p, 0))
                    if PRIORITY.get(echoed, 0) < PRIORITY.get(inherited, 0):
                        ladder["downgrades_blocked"] += 1
                    out_annos.append({**a, "provenance": winner or "ai"})
            else:
                out_annos = [{**a, "provenance": "ai"} for a in a2_annos]
            final = {
                "slug": slug, "wikipedia_title": title, "display_title": title,
                "schema_version": 3, "annotation_style": "theorem_article",
                "annotations": out_annos,
            }
            (ANNOT / f"{slug}.json").write_text(
                json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")

            # 6. render. render.py's CLI reads the LEGACY un-suffixed cache,
            # so with a pinned target_revid the anchor match runs in-process
            # against the revid-suffixed HTML instead (F1) — matched stats
            # must describe the revision the agents actually reviewed.
            if target_revid is None:
                rc, render_out = run_script("render.py", slug)
                if rc != 0 or not (OUT / f"{slug}.html").exists():
                    rec["error"] = "render_failed"
                    return rec
                mm = re.search(r"(\d+)/(\d+) matched", render_out)
                rec["matched"] = mm.group(0) if mm else None
            else:
                import render as _render
                src = _render.absolutize_wikipedia_urls(
                    (CACHE / f"{cache_slug}.html").read_text(encoding="utf-8"))
                _, flags = _render.wrap_annotations(src, final["annotations"])
                non_tomb = [i for i, a in enumerate(final["annotations"])
                            if a.get("status") != "rejected"]
                matched = sum(1 for i in non_tomb if flags[i])
                rec["matched"] = f"{matched}/{len(non_tomb)} matched"
            rec["n_annotations"] = len(final["annotations"])
            rec["ladder"] = ladder
            rec["cost_usd_equiv"] = round(
                (a1_meta.get("cost_usd_equiv") or 0) + (a2_meta.get("cost_usd_equiv") or 0), 3)
            rec["tokens"] = (a1_meta.get("tokens") or 0) + (a2_meta.get("tokens") or 0)
            rec["agent1_meta"] = a1_meta
            rec["agent2_meta"] = a2_meta
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        finally:
            rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


async def run(articles: list[dict], seed_decls: dict, concurrency: int,
              moderate: bool = False) -> int:
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    n_done = n_err = 0
    cost = 0.0
    tokens_total = 0
    lock = asyncio.Lock()
    state = {"consec_err": 0, "abort": False}
    ABORT_AFTER = 15  # consecutive window-exhaustion errors → stop, resume later

    with RUN_LOG.open("a", encoding="utf-8") as log:
        async def worker(a: dict):
            nonlocal n_done, n_err, cost, tokens_total
            if state["abort"]:
                return  # window died — skip cheaply, retried on next resume
            rec = await annotate_one(a, sem, seed_decls, moderate=moderate)
            async with lock:
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log.flush()
                n_done += 1
                err = rec.get("error")
                if err:
                    n_err += 1
                    low = err.lower()
                    if ("error result: success" in low or "rate" in low
                            or "limit" in low or "overloaded" in low):
                        state["consec_err"] += 1
                        if state["consec_err"] >= ABORT_AFTER and not state["abort"]:
                            state["abort"] = True
                            print(f"  ⚠ {state['consec_err']} consecutive window-"
                                  f"exhaustion errors — aborting; rerun resumes "
                                  f"after the window resets", flush=True)
                else:
                    state["consec_err"] = 0
                cost += rec.get("cost_usd_equiv") or 0
                tokens_total += rec.get("tokens") or 0
                elapsed = time.time() - t0
                rate = n_done / elapsed if elapsed else 0
                eta = (len(articles) - n_done) / rate if rate else 0
                status = rec.get("error") or rec.get("matched") or "ok"
                print(f"  [{n_done}/{len(articles)}] {rec['slug'][:40]:40s} "
                      f"{status:18s} cov={rec.get('coverage_pct')}% "
                      f"err={n_err} ~${cost:.2f} equiv {tokens_total/1e6:.2f}Mtok "
                      f"eta={eta/60:.0f}m", flush=True)

        await asyncio.gather(*(worker(a) for a in articles))

    print(f"\ndone — {n_done} processed, {n_err} errors, "
          f"{time.time()-t0:.0f}s, ~${cost:.2f} equiv, {tokens_total/1e6:.2f}M tokens"
          + ("  [ABORTED: window exhausted — rerun to resume]" if state["abort"] else ""))
    return 3 if state["abort"] else 0


def load_articles() -> tuple[list[dict], dict]:
    """Merge pilot + tier2 tagged concept articles. Returns (articles, seed_decls)."""
    titles: dict[str, dict] = {}
    seed: dict[str, str] = {}
    for f in ["pilot_tagged.jsonl", "tier2_tagged.jsonl"]:
        p = CATALOG_DATA / f
        if not p.exists():
            continue
        for line in p.open():
            r = json.loads(line)
            if r.get("is_human"):
                continue
            titles[r["title"]] = {"title": r["title"]}
            decls = r.get("mathlib_decls") or []
            if decls:
                seed[r["title"]] = ", ".join(
                    f"{d.get('decl')} ({d.get('module')})" for d in decls[:6])
    return list(titles.values()), seed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--only-matched", action="store_true",
                    help="Only articles that have prior Mathlib matches (840).")
    ap.add_argument("--force", action="store_true",
                    help="Re-run even if out/<slug>.html exists.")
    args = ap.parse_args()

    if not MATHLIB.exists():
        print(f"ERROR: mathlib4 not found at {MATHLIB}", file=sys.stderr)
        return 1
    for d in (CACHE, ANNOT, OUT):
        d.mkdir(parents=True, exist_ok=True)
    if _popped_key:
        print("(unset ANTHROPIC_API_KEY → Max-plan auth)")

    articles, seed_decls = load_articles()
    if args.only_matched:
        articles = [a for a in articles if a["title"] in seed_decls]

    # Resume: skip articles whose final render already exists.
    if not args.force:
        pending = [a for a in articles
                   if not (OUT / f"{make_slug(a['title'])}.html").exists()]
        skipped = len(articles) - len(pending)
        if skipped:
            print(f"resume: skipping {skipped} already-rendered articles")
        articles = pending

    if args.limit:
        articles = articles[: args.limit]

    print(f"processing {len(articles)} articles @ concurrency {args.concurrency}")
    return asyncio.run(run(articles, seed_decls, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
