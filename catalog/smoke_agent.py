#!/usr/bin/env python3
"""Smoke test: spawn one Claude agent under Max-subscription auth, ask it to
find Mathlib4 declarations matching the Wikipedia article "Vector space"."""
from __future__ import annotations

import os
import sys

# IMPORTANT: pop the API key BEFORE importing the SDK, so the spawned `claude`
# subprocess uses Max-subscription auth (the local `claude login` session)
# instead of billing to the API account associated with the key.
if os.environ.pop("ANTHROPIC_API_KEY", None):
    print("(unset ANTHROPIC_API_KEY for this process → routing via Max plan)")

import asyncio

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    query,
)

MATHLIB = "/Users/jack/Desktop/LEAN/mathlib4"

SYSTEM_PROMPT = """\
You are a research assistant for the WikiLean project. For one Wikipedia
mathematics article, identify Mathlib4 declarations (defs, theorems, lemmas,
structures, classes, instances) that formalize its central concept.

Mathlib4 is the current working directory. Only look in `Mathlib/`.

Process:
  1. From the article, identify the central concept.
  2. Use Grep to find candidate declaration heads in Mathlib (e.g. `^def `,
     `^theorem `, `^class `, `^structure `, or the concept's canonical Mathlib
     spelling like `MetricSpace`, `CauchySeq`).
  3. Use Read to verify candidates exist and match the concept.
  4. Report ONLY declarations you verified. Do NOT invent declaration names.
  5. Prefer 1-5 high-confidence decls over a long list of guesses.

OUTPUT FORMAT — your final reply must be ONLY one JSON object, no prose:

{
  "mathlib_decls": [
    {
      "decl": "<name as it appears in Mathlib, e.g. MetricSpace>",
      "module": "<dotted module path, e.g. Mathlib.Topology.MetricSpace.Basic>",
      "kind": "def" | "theorem" | "lemma" | "structure" | "class" | "instance" | "abbrev" | "inductive" | "other",
      "confidence": "high" | "medium" | "low",
      "evidence": "<relative path:line — short quote you grepped>"
    }
  ],
  "primary_decl": "<single most central decl name, or null>",
  "notes": "<at most one sentence>",
  "no_match_reason": null
}

If nothing exists, return mathlib_decls=[] and set no_match_reason to one of:
"not formalized", "too elementary", "not amenable to formalization",
"unclear scope", "other".
"""

USER_PROMPT = """Article: Vector space
Wikidata: Q125977
Class: GA / Importance: Top
P31: []

Lead:
In mathematics and physics, a vector space (also called a linear space) is a set
whose elements, often called vectors, can be added together and multiplied
("scaled") by numbers called scalars. The operations of vector addition and
scalar multiplication must satisfy certain requirements, called vector axioms.
Real vector spaces and complex vector spaces are kinds of vector spaces based on
different kinds of scalars: real numbers and complex numbers. Scalars can also
be, more generally, elements of any field.

Identify Mathlib declarations that formalize the central concept.
Reply with ONLY the JSON object specified — no other text.
"""


def short(block) -> str:
    if isinstance(block, TextBlock):
        t = block.text or ""
        return f"text({len(t)}ch): {t[:160]!r}{'...' if len(t) > 160 else ''}"
    if isinstance(block, ToolUseBlock):
        return f"tool_use({block.name}): {block.input}"
    if isinstance(block, ToolResultBlock):
        c = str(block.content) if block.content is not None else ""
        return f"tool_result(is_error={block.is_error}, {len(c)}ch)"
    if isinstance(block, ThinkingBlock):
        return f"thinking({len(block.thinking or '')}ch)"
    return f"{type(block).__name__}"


async def main() -> int:
    options = ClaudeAgentOptions(
        model="claude-opus-4-7",
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=["Read", "Grep", "Glob"],
        cwd=MATHLIB,
        permission_mode="bypassPermissions",
        max_turns=20,
    )
    final = None
    n_messages = 0
    async for msg in query(prompt=USER_PROMPT, options=options):
        n_messages += 1
        cls = type(msg).__name__
        if isinstance(msg, (AssistantMessage, UserMessage)):
            print(f"\n[{n_messages}] {cls}  ({len(msg.content)} block(s))")
            for b in msg.content:
                print(f"    · {short(b)}")
        elif isinstance(msg, SystemMessage):
            print(f"\n[{n_messages}] SystemMessage  subtype={getattr(msg, 'subtype', None)}")
        elif isinstance(msg, ResultMessage):
            final = msg
            print(f"\n[{n_messages}] ResultMessage")
        else:
            print(f"\n[{n_messages}] {cls}")

    print("\n==== FINAL ====")
    if final is None:
        print("no ResultMessage received")
        return 1
    for attr in ("subtype", "is_error", "duration_ms", "duration_api_ms",
                 "num_turns", "session_id", "total_cost_usd", "result"):
        v = getattr(final, attr, None)
        if attr == "result" and isinstance(v, str) and len(v) > 800:
            print(f"  {attr}: {v[:800]!r} ...({len(v)} chars total)")
        else:
            print(f"  {attr}: {v}")
    usage = getattr(final, "usage", None)
    if usage:
        print(f"  usage: {usage}")
    return 0 if not final.is_error else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
