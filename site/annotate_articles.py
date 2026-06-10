#!/usr/bin/env python3
"""WikiLean v3 article annotator — by definition/proposition, not by equation.

For each Wikipedia math article, identify the definitions and propositions
stated in the prose, and decide whether Mathlib4 has formalized each one.

Key differences from v2:
  - The annotation UNIT is a definition or proposition (semantic), not a
    display equation or theorem-box (syntactic).
  - A definition is formalized if Mathlib has a corresponding typeclass /
    structure / def, OR a standard invocation pattern (e.g. `[Module F V]`
    for a vector space).
  - A proposition is formalized if Mathlib has the statement OR a
    GENERALIZATION of it. Different proof technique does NOT downgrade the
    status — instead it gets a `proof_note`.
  - Per-annotation `provenance` field. On re-run, the agent regenerates
    entries with `provenance == "ai"` and leaves human/moderator entries
    untouched.

Inputs:
  catalog/data/{pilot,tier2}_tagged.jsonl    article-level Mathlib hints
  site/cache/<slug>.wikitext                  cached wikitext (auto-fetched)

Output:
  site/annotations/<slug>.json                v3-schema annotation file

Uses Claude Max-plan auth (the `claude login` session). ANTHROPIC_API_KEY is
popped before SDK import so a stray key doesn't redirect billing to the API.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

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
ROOT = HERE.parent
CACHE_DIR = HERE / "cache"
ANNOT_DIR = HERE / "annotations"
LOG_PATH = HERE / "data" / ".annotate_run.log"

PILOT_TAGGED = ROOT / "catalog" / "data" / "pilot_tagged.jsonl"
TIER2_TAGGED = ROOT / "catalog" / "data" / "tier2_tagged.jsonl"
MATHLIB = Path("/Users/jack/Desktop/LEAN/mathlib4")

WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = "WikiLean/0.3 (https://github.com/; jack.mccarthy.1@stonybrook.edu)"

SCHEMA_VERSION = 3
AGENT_TAG = f"annotate_articles.py@v{SCHEMA_VERSION}"

CLASS_RANK = {"FA": 0, "GA": 1, "A": 2, "B": 3, "C": 4, "Start": 5, "Stub": 6, "List": 7}
IMP_RANK = {"Top": 0, "High": 1, "Mid": 2, "Low": 3}


SPLITTER_PROMPT = """\
You are the SPLITTER agent for the WikiLean project. Your ONE job is to
read a Wikipedia mathematics article and enumerate every substantive
DEFINITION, PROPOSITION, and EXAMPLE it contains. You do NOT search
Mathlib, you do NOT assign formalization status — a separate Matcher
agent does that downstream.

Your single optimisation target is COMPLETENESS. Under-splitting cannot
be fixed downstream; over-splitting can (the Matcher can merge). When
in doubt, emit a SEPARATE item.

KINDS

  • "definition"  — the article introduces a mathematical object,
                    structure, relation, property, or named operator.
                    Includes invocation-style definitions
                    (e.g. "a vector space over F" = `[Module F V]`).

  • "proposition" — the article states a theorem, lemma, proposition,
                    corollary, formula, identity, or named property,
                    with or without proof.

  • "example"     — the article gives a specific instance, worked
                    example, or COUNTER-EXAMPLE that demonstrates a
                    definition or proposition. Always emit
                    counter-examples (they're the most informative
                    items for "is X formalized in Mathlib?").

DO NOT emit:
  proof intermediates · algebraic manipulation steps · biographical
  remarks · history-of-the-result paragraphs · references · "See also"
  sections · table-of-contents-style summaries.

PROCESS

  Read the wikitext top to bottom. For EACH paragraph (and each
  template box like {{math theorem|...}}), ask:
    — Is there a definition here?
    — Is there a proposition (a stated theorem / lemma / formula /
      claim that something equals / implies / converges to something)?
    — Is there an example or counter-example?
  A single paragraph commonly contains 2–3 distinct items. A theorem
  statement followed by "Equivalently, X" is TWO items. A definition
  + the formula that defines it is TWO items (or one with two anchors).

ANCHOR HINTS

  For each item, propose ONE OR MORE candidate anchors. The Matcher
  will use these to locate the item in the rendered HTML.

  Anchor shapes:
    {"type":"prose_snippet","section":"<heading>","snippet":"<text>"}
    {"type":"math_alttext","value":"<the math's alttext from wikitext>"}
    {"type":"prose_range","section":"<heading>","from":"<prose>",
     "to_math":"<math alttext>"}

  Rules:
    • If the item IS a named entity that the article wikilinks to
      ("Banach fixed-point theorem", "Peano existence theorem"), the
      snippet is JUST the entity name — no surrounding prose.
    • If the item is prose followed immediately by a displayed
      equation that IS the item, propose a prose_range from the prose
      start TO the math (`to_math` uses the equation's wikitext form).
    • If the item is a single displayed equation, use math_alttext.
    • If the item has two equivalent formulations (e.g., a recurrence
      AND an operator definition), propose BOTH anchors as two
      entries in candidate_anchors of ONE item.
    • Snippets must be plain English prose AS RENDERED — no `[[ ]]`,
      no `''italic''`, no `{{math|x}}`, no `&thinsp;`. Use rendered
      text only.

OUTPUT FORMAT — your final reply must be EXACTLY ONE JSON object,
no prose, no markdown fences:

{
  "items": [
    {
      "kind": "definition" | "proposition" | "example",
      "label": "<short title, ≤ 12 words>",
      "section": "<the section heading the item lives in>",
      "candidate_anchors": [<one or more anchor objects>],
      "rationale": "<one sentence: why this is a distinct item, esp. if it shares a paragraph with a sibling item>"
    },
    ...
  ]
}

BE GENEROUS. If a paragraph mentions Banach FPT and then states a
contraction lemma and then derives a fixed-point existence claim,
that's THREE items. Emit all three.
"""


SYSTEM_PROMPT = """\
You are a research assistant for the WikiLean project. For one Wikipedia
mathematics article, identify the DEFINITIONS, PROPOSITIONS, and EXAMPLES
stated in the article, and decide whether Mathlib4 has formalized each
one.

The current working directory is a local Mathlib4 clone. Use Read, Grep, and
Glob on `Mathlib/` to discover declarations.

You ALSO have WebFetch to read the rendered Mathlib4 docs. URL pattern:

    https://leanprover-community.github.io/mathlib4_docs/<MODULE_WITH_SLASHES>.html#<DECL_NAME>

  e.g. for `IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt`
  in module `Mathlib.Analysis.ODE.PicardLindelof`:
    https://leanprover-community.github.io/mathlib4_docs/Mathlib/Analysis/ODE/PicardLindelof.html#IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt

A module's page lists every decl in that module with full signature and
docstring — a great way to discover related decls and confirm meanings.

VERIFICATION RULE: before citing a decl in your output, you SHOULD have
checked it in at least one of these two ways:
  (a) read its definition in the local `Mathlib/...lean` source, OR
  (b) WebFetched its docs URL and read the rendered signature/docstring.
Prefer (b) when the decl's meaning isn't obvious from its name, when you
want to confirm an arity/hypothesis signature, or when comparing the
article's statement against Mathlib's formal one. Prefer (a) when you
need to grep across modules to FIND the decl in the first place.

WHAT TO ANNOTATE

Three kinds of items:

  - "definition"  — the article introduces a mathematical object, structure,
                    relation, or property by definition (e.g. "a *metric
                    space* is a set X with a function d : X × X → ℝ such
                    that …", or "a function f is *continuous at p* iff …").

  - "proposition" — the article states a theorem, lemma, proposition,
                    corollary, or named formula, with or without proof
                    (e.g. "Theorem (Banach fixed-point). Every contraction
                    on a complete metric space has a unique fixed point.").

  - "example"     — the article gives a specific instance or COUNTER-EXAMPLE
                    that demonstrates a definition or proposition. Always
                    annotate counter-examples (e.g. "an ODE whose right-hand
                    side fails Lipschitz, exhibiting non-uniqueness"). For
                    counter-examples especially, the annotation should
                    state whether the specific counter-example has been
                    formalized in Mathlib (it usually has NOT — Mathlib
                    formalizes positive theorems, not counter-examples).

DO NOT annotate proof intermediates, algebraic-manipulation steps,
historical/biographical remarks, or generalizations briefly gestured at
without being stated. Aim for the ~5–20 substantive items in the article,
not 50+.

STATUS RULES — BE GENEROUS

For a DEFINITION, status = "formalized" if Mathlib has ANY of:
  • a typeclass that captures the concept (`Group`, `Module`, `CommRing`)
  • a structure (`MetricSpace`, `NormedAddCommGroup`)
  • a definition (`Function.Bijective`, `CauchySeq`)
  • a canonical invocation pattern, e.g. "a vector space over F" is the
    pattern `[Field F] [AddCommGroup V] [Module F V]`. When the article's
    definition is captured by such a *combination* of Mathlib pieces rather
    than a single decl, use match_kind = "invocation" and list one
    representative decl (e.g. `Module`) plus mention the full pattern in
    `note`.

For a PROPOSITION, status = "formalized" if Mathlib has ANY of:
  • a theorem/lemma with the same statement, OR
  • a strictly more GENERAL theorem (whose specialization to the article's
    hypotheses yields the article's statement). The proof technique does
    NOT need to match. If Mathlib's proof technique is materially different
    from the article's, set `proof_note` to flag it; do NOT downgrade the
    status.

Use status = "partial" only if Mathlib has something narrower or sideways:
  • only one direction of an iff, only special case the article generalizes,
    only the statement without a proof, etc.

Use status = "not_formalized" only when no relevant Mathlib decl exists.

PROCESS — FOLLOW THESE STEPS IN ORDER

  Step 1. ENUMERATE, section by section.

     Read the wikitext from top to bottom. For EACH `== Section ==` (skip
     References, See also, External links, Notes, Citations), list every
     substantive item in that section:
       • each DEFINITION (a concept being introduced)
       • each PROPOSITION (a theorem, lemma, formula, named result)
       • each EXAMPLE (worked instance, counter-example)
     A single Wikipedia paragraph commonly contains MULTIPLE items — do
     NOT merge them. A theorem statement, the definition it introduces,
     and a variant special case are three separate annotations even
     when they live in one `<p>`.

  Step 2. MATCH against Mathlib.

     For each enumerated item:
       a. Check the prior tagger's decl list first.
       b. If nothing in the list captures the item exactly OR as a
          generalization, search the Mathlib4 source via Grep on
          declaration heads (`^theorem `, `^lemma `, `^def `, `^class `,
          `^structure `) or on canonical name patterns derived from the
          concept. Open candidate files with Read to confirm.
       c. ALWAYS prefer the most general matching Mathlib decl. If
          Mathlib's `ContractingWith.tendsto_iterate_fixedPoint` covers
          the article's "Picard iterates converge to a solution", that's
          a `formalized` / `generalization` match — NOT `partial`.

  Step 3. PROOF-DIVERGENCE NOTES, not status downgrades.

     For propositions, status is determined by WHETHER Mathlib has the
     statement, NOT by HOW Mathlib proves it. If Mathlib uses a different
     strategy, set `status: "formalized"` AND populate `proof_note` with
     a one-sentence diff. Examples:
       — article shrinks an interval; Mathlib uses iterated contractions
       — article uses subsequences; Mathlib uses filter cluster points
       — article uses induction; Mathlib uses well-founded recursion

  Step 4. ANCHOR CHOICE — pick the tightest form that captures the idea.

     This is the most-skipped step. Decision tree:

       (a) Item IS a single named entity that the article wikilinks to
           (e.g. "Banach fixed-point theorem", "Peano existence theorem"):
              anchor: {section, snippet: "<just the entity name>"}
           The renderer wraps just the wikilink — a tight inline highlight.

       (b) Item is stated as prose IMMEDIATELY FOLLOWED by a displayed
           equation that IS that statement:
              anchors: [
                {type: "prose_range", section, from: "<prose start>",
                 to_math: "<the equation's alttext>"}
              ]
           Or, if the prose and the equation should each be highlighted
           separately:
              anchors: [
                {section, snippet: "<prose substring>"},
                {type: "math_alttext", value: "<equation alttext>"}
              ]

       (c) Item is a definition with TWO equivalent formulations (e.g.,
           a recurrence + an operator form):
              anchors: [
                {type: "math_alttext", value: "<formula 1>"},
                {type: "math_alttext", value: "<formula 2>"}
              ]

       (d) Item is a sentence-or-two of pure prose with no math:
              anchor: {section, snippet: "<distinctive prose substring>"}

       (e) Item is a single displayed equation:
              anchor: {type: "math_alttext", value: "<alttext>"}

  Step 5. SELF-REVIEW.

     Before emitting, scan each section's contribution and ask:
       — Did I miss any DEFINITION, PROPOSITION, or COUNTER-EXAMPLE?
       — For every "Theorem (...)" or "Definition" or "Lemma" labeled
         in the article, is there a corresponding annotation?
       — For each counter-example I marked `not_formalized`, did I
         briefly grep Mathlib to confirm absence?
       — Did I unify prose-and-equation pairs via `anchors: [...]`?
       — Did I downgrade any proposition to `partial` solely because
         Mathlib's proof differed? If yes, promote to `formalized` and
         add a `proof_note`.

WORKED EXAMPLE (Picard–Lindelöf article — abbreviated for illustration)

In the "Theorem" section, ONE paragraph yields THREE annotations:
  • Initial value problem — definition, `partial`, match_kind=`invocation`,
    decl `HasDerivWithinAt`, note: "Mathlib has no `IVP` structure; an IVP
    is the pattern `α t₀ = y₀ ∧ ∀ t, HasDerivAt α (f t (α t)) t`".
  • Picard–Lindelöf theorem (local existence) — proposition, `formalized`,
    decl `IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt`.
  • Picard–Lindelöf for C¹ vector fields — proposition, `formalized`,
    match_kind=`special_case`, decl `ContDiffAt.exists_forall_mem_…`.

In "Proof sketch", a proposition "solutions satisfy the integral equation"
uses TWO anchors — the prose sentence AND the displayed equation — sharing
one annotation:
  anchors: [
    {section: "Proof sketch",
     snippet: "any solution to the differential equation must also satisfy"},
    {type: "math_alttext",
     value: "{\\displaystyle y(t)-y(t_{0})=\\int _{t_{0}}^{t}f(s,y(s))\\,ds.}"}
  ]

In "Example of non-uniqueness", the dy/dt = a·y^{2/3} counter-example is
kind=`example`, status=`not_formalized`, with a `prose_range` extending
from "By contrast for an equation" through the displayed solution
y(t)=(at/3)³.

ANCHOR FORMAT

Each annotation has an `anchor` (singular) or `anchors` (list of two or
more) that lets the renderer locate it in the article's rendered HTML.

Use `anchor` (singular) for the common case — one def/prop highlighted in
one place:

    "anchor": {
      "section": "<the section heading the item appears in>",
      "snippet": "<a verbatim ~30–100 char substring of the article's
                   PROSE that uniquely identifies this item within the
                   section. Pick plain English text, NOT math. Example:
                   'is one for which every Cauchy sequence converges'>"
    }

Use `anchors` (list) when one conceptual def/prop should highlight BOTH a
prose paragraph AND its accompanying displayed equation (or any other
combined pair). All anchors in the list share the same label, decl, note,
etc., so the user hovers any of them and sees one tooltip:

    "anchors": [
      { "section": "Proof sketch",
        "snippet": "any solution to the differential equation must also satisfy" },
      { "section": "Proof sketch",
        "snippet": "the integral equation" }
    ]

The snippet MUST appear literally in the article's RENDERED PROSE (what a
reader sees, not the wikitext source). Specifically:

  - DO NOT include `[[...]]` wiki-link syntax (write `Banach fixed-point
    theorem`, not `[[Banach fixed-point theorem]]`).
  - DO NOT include `''italic''` or `'''bold'''` markers.
  - DO NOT include `{{math|x}}` or other template syntax — use the rendered
    text (e.g. write `f` instead of `{{math|''f''}}`).
  - DO NOT include HTML entities like `&thinsp;` — use plain space.

CHOOSING SNIPPET WIDTH

The renderer wraps the SMALLEST element whose text contains your snippet,
so the snippet's WIDTH controls the size of the highlight box:

  - For a proposition referenced by a NAMED ENTITY that the article links
    to (e.g. a theorem with its own Wikipedia article), make the snippet
    just the name: `"Banach fixed-point theorem"`. The renderer will wrap
    only the wikilink — a tight, focused highlight.

  - For a definition or proposition stated IN PROSE (not via a wikilink),
    pick a distinctive phrase from inside the relevant sentence:
    `"is one for which every Cauchy sequence converges"`. The renderer
    will wrap the surrounding paragraph.

If you want the WHOLE PARAGRAPH highlighted, use a long prose substring.
If you want JUST A LINK highlighted, use the link's text only. Default to
tight unless the definition/proposition really is the paragraph itself.

OUTPUT FORMAT — your final reply must be EXACTLY one JSON object, no prose:

{
  "annotation_style": "theorem_article" | "definition_article" | "survey" | "other",
  "annotations": [
    {
      "kind": "definition" | "proposition" | "example",
      "label": "<short name, e.g. 'Cauchy sequence' or 'Banach fixed-point theorem'>",
      "anchor": { "section": "<heading>", "snippet": "<prose substring>" },
      "status": "formalized" | "partial" | "not_formalized",
      "mathlib": {
        "decl":       "<Mathlib decl name>"            | null,
        "module":     "<dotted module path>"           | null,
        "match_kind": "exact" | "generalization" | "special_case" | "invocation" | null
      },
      "note":       "<one short sentence; for definitions, mention the canonical invocation pattern if relevant>",
      "proof_note": "<optional; for propositions where Mathlib's proof technique materially differs from the article's>",
      "provenance": "ai"
    }
  ]
}

Hard rules:
  - Every annotation MUST have provenance = "ai".
  - Never list a decl whose name you have not seen via Grep/Read in the
    current mathlib4 source tree.
  - Be generous toward "formalized" per the rules above; do NOT default to
    "not_formalized" just because the article's exact wording differs.
"""


MATCHER_PROMPT = """\
You are the MATCHER agent for the WikiLean project. A prior SPLITTER agent
has already enumerated every substantive definition, proposition, and
example in one Wikipedia mathematics article. Your job: for each
enumerated item, decide whether Mathlib4 has formalized it, and produce
one structured annotation per item.

You do NOT need to identify the items yourself — the Splitter did that.
Focus 100% of your effort on accurate Mathlib matching and verification.

The current working directory is a local Mathlib4 clone. Use Read, Grep,
and Glob on `Mathlib/` to discover declarations.

You ALSO have WebFetch. URL pattern for Mathlib4 docs:
    https://leanprover-community.github.io/mathlib4_docs/<MODULE_WITH_SLASHES>.html#<DECL_NAME>

  e.g. for `IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt`
  in module `Mathlib.Analysis.ODE.PicardLindelof`:
    https://leanprover-community.github.io/mathlib4_docs/Mathlib/Analysis/ODE/PicardLindelof.html#IsPicardLindelof.exists_eq_forall_mem_Icc_hasDerivWithinAt

VERIFICATION RULE — IMPORTANT
  Before citing a decl in your output, you MUST have either:
    (a) read its definition in the local `Mathlib/...lean` source, OR
    (b) WebFetched its docs URL and read the signature there.
  Never cite a decl name you have not verified. (The prior single-agent
  pipeline cited names like `toPartialHomeomorph` that didn't exist —
  the real name was `toOpenPartialHomeomorph`. Verify each cited name.)

STATUS RULES — BE GENEROUS

For a DEFINITION, status = "formalized" if Mathlib has ANY of:
  • a typeclass that captures the concept (`Group`, `Module`, `CommRing`)
  • a structure (`MetricSpace`, `NormedAddCommGroup`)
  • a definition (`Function.Bijective`, `CauchySeq`)
  • a canonical invocation pattern, e.g. "a vector space over F" is the
    pattern `[Field F] [AddCommGroup V] [Module F V]`. When the article's
    definition is captured by such a *combination* of Mathlib pieces
    rather than a single decl, use match_kind = "invocation".

For a PROPOSITION, status = "formalized" if Mathlib has ANY of:
  • a theorem/lemma with the same statement, OR
  • a strictly more GENERAL theorem (whose specialization yields the
    article's claim). The proof technique does NOT need to match. If
    Mathlib's proof technique is materially different, set `proof_note`
    — do NOT downgrade the status to "partial" for proof differences.

Use status = "partial" only if Mathlib has something narrower or sideways:
only one direction of an iff, only a special case, only the statement
without a proof, etc.

For an EXAMPLE (especially counter-examples), status is almost always
"not_formalized" — Mathlib formalizes positive theorems, not
counter-examples. Don't search hard for these.

ANCHOR CHOICE

The Splitter proposed `candidate_anchors` for each item. You should:
  - USE the Splitter's candidate_anchors as-is when they look good.
  - REFINE if you find a better anchor (e.g. a tighter named-entity
    snippet, or a multi-anchor pair when the prose + a displayed equation
    should both be highlighted).

PROCESS

  Step 1. For each enumerated item, find the matching Mathlib decl:
     a. First check the pre-tagged decl list provided in the prompt.
     b. If nothing matches, Grep `Mathlib/` for plausible decl heads or
        canonical name patterns.
     c. VERIFY each candidate before listing — Read its source or
        WebFetch its docs URL.

  Step 2. PROOF-DIVERGENCE NOTES, not status downgrades.
     If Mathlib uses a different proof strategy for a proposition, set
     `status: "formalized"` AND populate `proof_note`. Do not demote.

  Step 3. Set the anchor (use or refine the Splitter's proposal).

  Step 4. SELF-REVIEW.
     — Did I verify every cited decl exists in Mathlib?
     — Did I downgrade any proposition to "partial" only because Mathlib's
       proof differed? If yes, promote to "formalized" with a proof_note.
     — Did I emit exactly one annotation per Splitter item, in the same
       order?

OUTPUT FORMAT — EXACTLY ONE JSON OBJECT, NO PROSE, NO MARKDOWN:

{
  "annotations": [
    {
      "kind": "definition" | "proposition" | "example",
      "label": "<short title, ≤ 12 words>",
      "anchor"  | "anchors": <object or list — the Splitter's candidate
                              anchor(s), refined as needed>,
      "status": "formalized" | "partial" | "not_formalized",
      "mathlib": {
        "decl":       "<verified Mathlib name>" | null,
        "module":     "<dotted module path>"    | null,
        "match_kind": "exact" | "generalization" | "special_case" | "invocation" | null
      },
      "note":       "<one short sentence>",
      "proof_note": "<optional; when Mathlib proves the proposition differently>",
      "provenance": "ai"
    },
    ...
  ]
}

The output `annotations` array length MUST equal the input enumeration
items array length, in the same order.
"""


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    s = title.replace(" ", "_")
    s = s.replace("–", "-").replace("—", "-")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\-.]", "", s)
    return s


# ---------------------------------------------------------------------------
# Fetch wikitext (compact, structured input for the agent)
# ---------------------------------------------------------------------------

_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})


def fetch_wikitext(slug: str, wp_title: str) -> str:
    cache_path = CACHE_DIR / f"{slug}.wikitext"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    params = {
        "action": "parse", "page": wp_title, "prop": "wikitext",
        "format": "json", "formatversion": "2",
    }
    r = _HTTP.get(WIKI_API, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "parse" not in data:
        raise RuntimeError(f"MediaWiki error for {wp_title!r}: {data.get('error') or data}")
    text = data["parse"]["wikitext"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# Agent orchestration
# ---------------------------------------------------------------------------

def build_splitter_prompt(article: dict, wikitext: str) -> str:
    return (
        f"Article: {article['title']}\n"
        f"Class/Importance: {article.get('class')}/{article.get('importance')}\n\n"
        f"Article wikitext (size={len(wikitext)} chars):\n"
        f"---BEGIN WIKITEXT---\n{wikitext}\n---END WIKITEXT---\n\n"
        "Enumerate every substantive definition, proposition, and example "
        "(including counter-examples) in this article. Be GENEROUS in "
        "splitting — a paragraph commonly contains 2–3 distinct items. "
        "Emit ONLY the JSON object specified in the system prompt."
    )


def build_matcher_prompt(article: dict, wikitext: str, enumeration: dict) -> str:
    decls = article.get("mathlib_decls") or []
    items = enumeration.get("items", [])
    return (
        f"Article: {article['title']}\n"
        f"Class/Importance: {article.get('class')}/{article.get('importance')}\n"
        f"Prior tagger notes: {article.get('notes') or '(none)'}\n\n"
        f"SPLITTER's enumeration ({len(items)} items):\n"
        f"{json.dumps(enumeration, indent=2, ensure_ascii=False)}\n\n"
        f"Pre-tagged Mathlib decls related to this article ({len(decls)}):\n"
        f"{json.dumps(decls, indent=2, ensure_ascii=False)}\n\n"
        f"Article wikitext (for context, size={len(wikitext)} chars):\n"
        f"---BEGIN WIKITEXT---\n{wikitext}\n---END WIKITEXT---\n\n"
        "For each enumerated item, produce one annotation with verified "
        "Mathlib decl (if any), status, note, and (refined) anchor. "
        "VERIFY every decl name via Read or WebFetch before listing it. "
        "Emit ONLY the JSON object specified in the system prompt."
    )


def build_user_prompt(article: dict, wikitext: str) -> str:
    decls = article.get("mathlib_decls") or []
    return (
        f"Article: {article['title']}\n"
        f"Class/Importance: {article.get('class')}/{article.get('importance')}\n"
        f"Prior tagger summary: {article.get('notes') or '(none)'}\n"
        f"Primary decl (from tagger): {article.get('primary_decl')!r}\n\n"
        f"Mathlib decls related to this article ({len(decls)}, from a prior "
        f"article-level pass — use as a starting point, search for more if "
        f"needed):\n"
        f"{json.dumps(decls, indent=2, ensure_ascii=False)}\n\n"
        f"Article wikitext (size={len(wikitext)} chars):\n"
        f"---BEGIN WIKITEXT---\n{wikitext}\n---END WIKITEXT---\n\n"
        "Identify the substantive definitions and propositions stated in the "
        "article. For each, search Mathlib (verifying with Read) to decide "
        "if it is formalized (be generous — generalizations count, "
        "different proofs count). Emit ONLY the JSON object specified in "
        "the system prompt."
    )


def parse_json(text: str) -> dict | None:
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
                    return json.loads(text[start: i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def merge_with_existing(existing: dict | None, new: dict, title: str, slug: str) -> dict:
    """Replace AI-authored entries; preserve any with provenance != 'ai'."""
    kept: list[dict] = []
    if existing:
        for a in existing.get("annotations", []):
            if a.get("provenance") != "ai":
                kept.append(a)
    ai_entries = list(new.get("annotations", []))
    for a in ai_entries:
        a["provenance"] = "ai"
    out = {
        "slug": slug,
        "wikipedia_title": title,
        "display_title": title,
        "schema_version": SCHEMA_VERSION,
        "annotation_style": new.get("annotation_style"),
        "last_ai_run": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "annotations": kept + ai_entries,
    }
    return out


async def _run_agent_once(prompt: str, options: ClaudeAgentOptions) -> tuple[str, dict]:
    """Run one SDK query, return (last_text, meta_dict)."""
    last_text = ""
    result_obj: ResultMessage | None = None
    n_tool = 0
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    last_text = b.text or last_text
                elif isinstance(b, ToolUseBlock):
                    n_tool += 1
        elif isinstance(msg, ResultMessage):
            result_obj = msg
            if msg.result:
                last_text = msg.result
    meta: dict = {}
    if result_obj is not None:
        meta = {
            "num_turns": getattr(result_obj, "num_turns", None),
            "duration_ms": getattr(result_obj, "duration_ms", None),
            "n_tool_calls": n_tool,
            "total_cost_usd": getattr(result_obj, "total_cost_usd", None),
            "is_error": getattr(result_obj, "is_error", None),
        }
    return last_text, meta


async def annotate_one_split(
    article: dict,
    splitter_options: ClaudeAgentOptions,
    matcher_options: ClaudeAgentOptions,
    out_dir: Path,
    sem: asyncio.Semaphore,
) -> dict:
    """Two-stage pipeline: Splitter enumerates items, Matcher matches to Mathlib."""
    async with sem:
        t0 = time.time()
        title = article["title"]
        slug = slugify(title)
        try:
            wikitext = fetch_wikitext(slug, title)
        except Exception as e:
            return {"slug": slug, "title": title,
                    "error": f"fetch_failed: {type(e).__name__}: {e}",
                    "elapsed_s": round(time.time() - t0, 2)}

        # ----- Stage 1: Splitter -----
        try:
            splitter_text, splitter_meta = await _run_agent_once(
                build_splitter_prompt(article, wikitext), splitter_options,
            )
        except Exception as e:
            return {"slug": slug, "title": title,
                    "error": f"splitter_failed: {type(e).__name__}: {e}",
                    "elapsed_s": round(time.time() - t0, 2)}

        enumeration = parse_json(splitter_text)
        if enumeration is None or "items" not in enumeration:
            return {"slug": slug, "title": title,
                    "error": "splitter_no_json",
                    "raw_splitter": splitter_text[:1500],
                    "agent_meta": {"splitter": splitter_meta},
                    "elapsed_s": round(time.time() - t0, 2)}

        # Persist the Splitter's enumeration for inspection.
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{slug}.splitter.json").write_text(
            json.dumps(enumeration, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # ----- Stage 2: Matcher -----
        try:
            matcher_text, matcher_meta = await _run_agent_once(
                build_matcher_prompt(article, wikitext, enumeration), matcher_options,
            )
        except Exception as e:
            return {"slug": slug, "title": title,
                    "error": f"matcher_failed: {type(e).__name__}: {e}",
                    "agent_meta": {"splitter": splitter_meta},
                    "elapsed_s": round(time.time() - t0, 2)}

        annotations = parse_json(matcher_text)
        if annotations is None or "annotations" not in annotations:
            return {"slug": slug, "title": title,
                    "error": "matcher_no_json",
                    "raw_matcher": matcher_text[:1500],
                    "agent_meta": {"splitter": splitter_meta, "matcher": matcher_meta},
                    "elapsed_s": round(time.time() - t0, 2)}

        # Merge with any existing file (preserving non-AI entries).
        out_path = out_dir / f"{slug}.json"
        existing = None
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
            except json.JSONDecodeError:
                pass
        merged = merge_with_existing(existing, annotations, title, slug)
        out_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        total_cost = (
            (splitter_meta.get("total_cost_usd") or 0)
            + (matcher_meta.get("total_cost_usd") or 0)
        )
        return {"slug": slug, "title": title,
                "n_items_split": len(enumeration.get("items", [])),
                "n_annotations": len(merged["annotations"]),
                "agent_meta": {
                    "splitter": splitter_meta,
                    "matcher": matcher_meta,
                    "total_cost_usd": total_cost,
                },
                "elapsed_s": round(time.time() - t0, 2)}


async def annotate_one(
    article: dict,
    options: ClaudeAgentOptions,
    out_dir: Path,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        t0 = time.time()
        title = article["title"]
        slug = slugify(title)
        last_text = ""
        result_obj: ResultMessage | None = None
        n_tool = 0
        try:
            wikitext = fetch_wikitext(slug, title)
            async for msg in query(
                prompt=build_user_prompt(article, wikitext), options=options,
            ):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            last_text = b.text or last_text
                        elif isinstance(b, ToolUseBlock):
                            n_tool += 1
                elif isinstance(msg, ResultMessage):
                    result_obj = msg
                    if msg.result:
                        last_text = msg.result
        except Exception as e:
            return {"slug": slug, "title": title,
                    "error": f"{type(e).__name__}: {e}",
                    "elapsed_s": round(time.time() - t0, 2)}

        parsed = parse_json(last_text)
        meta: dict = {}
        if result_obj is not None:
            meta = {
                "num_turns": getattr(result_obj, "num_turns", None),
                "duration_ms": getattr(result_obj, "duration_ms", None),
                "n_tool_calls": n_tool,
                "total_cost_usd": getattr(result_obj, "total_cost_usd", None),
                "is_error": getattr(result_obj, "is_error", None),
            }
        if parsed is None or "annotations" not in parsed:
            return {"slug": slug, "title": title, "error": "no_json_in_result",
                    "raw_result": last_text[:1500],
                    "agent_meta": meta,
                    "elapsed_s": round(time.time() - t0, 2)}

        # Merge with any existing file (preserving non-AI entries).
        out_path = out_dir / f"{slug}.json"
        existing = None
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
            except json.JSONDecodeError:
                pass
        merged = merge_with_existing(existing, parsed, title, slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return {"slug": slug, "title": title,
                "n_annotations": len(merged["annotations"]),
                "n_ai": sum(1 for a in merged["annotations"] if a.get("provenance") == "ai"),
                "agent_meta": meta,
                "elapsed_s": round(time.time() - t0, 2)}


async def run(articles: list[dict], out_dir: Path,
              options: ClaudeAgentOptions | None,
              splitter_options: ClaudeAgentOptions | None,
              matcher_options: ClaudeAgentOptions | None,
              concurrency: int, use_split: bool) -> int:
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    n_done = 0
    n_err = 0
    cost = 0.0
    log_lock = asyncio.Lock()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    with LOG_PATH.open("a", encoding="utf-8") as log_f:

        async def worker(a: dict) -> None:
            nonlocal n_done, n_err, cost
            if use_split:
                rec = await annotate_one_split(
                    a, splitter_options, matcher_options, out_dir, sem)
            else:
                rec = await annotate_one(a, options, out_dir, sem)
            async with log_lock:
                log_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log_f.flush()
                n_done += 1
                if rec.get("error"):
                    n_err += 1
                meta = rec.get("agent_meta") or {}
                if meta.get("total_cost_usd"):
                    cost += float(meta["total_cost_usd"])
                elapsed = time.time() - t0
                rate = n_done / elapsed if elapsed else 0
                eta = (len(articles) - n_done) / rate if rate else 0
                n_ann = rec.get("n_annotations", "?")
                err = f" ERROR:{rec['error'][:40]}" if rec.get("error") else ""
                print(
                    f"  [{n_done}/{len(articles)}] {rec['title']!r:45s} "
                    f"n={n_ann} err={n_err} cost~${cost:.2f} "
                    f"eta={eta:.0f}s{err}",
                    flush=True,
                )

        await asyncio.gather(*(worker(a) for a in articles))

    print(
        f"\ndone — {n_done} processed ({n_err} errors) in "
        f"{time.time() - t0:.1f}s, cost~${cost:.2f}"
    )
    return 0


# ---------------------------------------------------------------------------
# Input loading & ordering
# ---------------------------------------------------------------------------

def load_tagged() -> list[dict]:
    out: list[dict] = []
    seen = set()
    for p in (PILOT_TAGGED, TIER2_TAGGED):
        if not p.exists():
            continue
        for line in p.open():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("mathlib_decls"):
                continue
            if r["title"] in seen:
                continue
            seen.add(r["title"])
            out.append(r)
    return out


def quality_key(a: dict) -> tuple:
    return (CLASS_RANK.get(a.get("class"), 99),
            IMP_RANK.get(a.get("importance"), 99),
            a["title"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ANNOT_DIR),
                    help="Where per-article JSON files are written.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap on number of articles to process.")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--max-turns", type=int, default=40,
                    help="Generous turn budget — the agent reads wikitext "
                         "and may grep mathlib4 many times.")
    ap.add_argument("--smoke", type=str, default=None,
                    help="Process exactly one title; bypass resume filter.")
    ap.add_argument("--force", action="store_true",
                    help="Re-annotate articles whose files already exist "
                         "(without --force, existing AI entries are still "
                         "regenerated; --force just disables the file-level "
                         "resume skip).")
    ap.add_argument("--split", action="store_true",
                    help="Use the two-agent (Splitter + Matcher) pipeline. "
                         "Splitter enumerates def/prop/example items without "
                         "Mathlib search; Matcher then verifies each against "
                         "Mathlib (with WebFetch on docs).")
    args = ap.parse_args()

    if _popped_key:
        print("(unset ANTHROPIC_API_KEY for this process → using Max-plan auth)")
    if not MATHLIB.exists():
        print(f"ERROR: mathlib4 not found at {MATHLIB}", file=sys.stderr)
        return 1

    articles = load_tagged()
    if args.smoke:
        match = next((a for a in articles if a["title"] == args.smoke), None)
        if not match:
            print(f"ERROR: no tagged article titled {args.smoke!r}", file=sys.stderr)
            return 1
        pending = [match]
    else:
        articles.sort(key=quality_key)
        out_dir = Path(args.out_dir)
        if args.force:
            done = set()
        else:
            done = {p.stem for p in out_dir.glob("*.json")} if out_dir.exists() else set()
        pending = [a for a in articles if slugify(a["title"]) not in done]
        print(f"matched articles: {len(articles)}  "
              f"already annotated: {len(done)}  "
              f"pending: {len(pending)}")
        if args.limit:
            pending = pending[: args.limit]
    if not pending:
        return 0

    if args.split:
        # Two-agent pipeline.
        # Splitter doesn't need mathlib4 — it just enumerates from wikitext.
        splitter_options = ClaudeAgentOptions(
            model=args.model,
            system_prompt=SPLITTER_PROMPT,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=6,  # the Splitter only emits one structured response
        )
        matcher_options = ClaudeAgentOptions(
            model=args.model,
            system_prompt=MATCHER_PROMPT,
            allowed_tools=["Read", "Grep", "Glob", "WebFetch"],
            cwd=str(MATHLIB),
            permission_mode="bypassPermissions",
            max_turns=args.max_turns,
        )
        return asyncio.run(run(
            pending, Path(args.out_dir),
            options=None,
            splitter_options=splitter_options,
            matcher_options=matcher_options,
            concurrency=args.concurrency,
            use_split=True,
        ))

    # Single-agent pipeline (default).
    options = ClaudeAgentOptions(
        model=args.model,
        system_prompt=SYSTEM_PROMPT,
        # WebFetch so the agent can verify each cited decl against the
        # rendered Mathlib4 docs.
        allowed_tools=["Read", "Grep", "Glob", "WebFetch"],
        cwd=str(MATHLIB),
        permission_mode="bypassPermissions",
        max_turns=args.max_turns,
    )
    return asyncio.run(run(
        pending, Path(args.out_dir),
        options=options,
        splitter_options=None,
        matcher_options=None,
        concurrency=args.concurrency,
        use_split=False,
    ))


if __name__ == "__main__":
    sys.exit(main())
