#!/usr/bin/env python3
"""Produce a stratified validation sheet (markdown) for hand-checking taggings.

Reads pilot_tagged.jsonl + tier2_tagged.jsonl. Writes data/validation.md with
clickable links to Wikipedia and Mathlib source. The user fills in per-decl
marks (Y/N/P/?) and an overall article verdict; parse_validation.py later
extracts stats.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
PILOT = HERE / "data" / "pilot_tagged.jsonl"
TIER2 = HERE / "data" / "tier2_tagged.jsonl"
OUT = HERE / "data" / "validation.md"

MATHLIB_BLOB = "https://github.com/leanprover-community/mathlib4/blob/master"
WIKI_BASE = "https://en.wikipedia.org/wiki/"


def load(path: Path, tier_label: str) -> list[dict]:
    recs = []
    for line in path.open():
        r = json.loads(line)
        r["_tier"] = tier_label
        recs.append(r)
    return recs


def wiki_link(title: str) -> str:
    return WIKI_BASE + urllib.parse.quote(title.replace(" ", "_"))


# Evidence format: "Mathlib/Foo/Bar.lean:42 — text"  (em-dash) or with hyphen.
_EVIDENCE_PATH = re.compile(r"(Mathlib/[\w/]+\.lean)(?::(\d+))?")


def mathlib_link(decl: dict) -> str:
    """Best-effort URL to where this decl is defined.

    Prefer `module` (always points at the decl's home file). Only inherit the
    line number from `evidence` when the evidence's file path matches the
    module — otherwise the evidence is a cross-reference (e.g. a docstring in
    a different file that mentions this decl) and its line is misleading.
    """
    module = decl.get("module", "")
    evidence = decl.get("evidence", "") or ""
    canonical = (module.replace(".", "/") + ".lean") if module else ""
    m = _EVIDENCE_PATH.search(evidence)

    if canonical:
        line = m.group(2) if m and m.group(1) == canonical else None
        anchor = f"#L{line}" if line else ""
        return f"{MATHLIB_BLOB}/{canonical}{anchor}"

    if m:
        anchor = f"#L{m.group(2)}" if m.group(2) else ""
        return f"{MATHLIB_BLOB}/{m.group(1)}{anchor}"
    return ""


def sample(recs: list[dict], n: int, rng: random.Random) -> list[dict]:
    return rng.sample(recs, min(n, len(recs)))


def render_matched(rec: dict, idx: int) -> str:
    decls = rec.get("mathlib_decls") or []
    lines = [
        f"### {idx}. [{rec['title']}]({wiki_link(rec['title'])}) "
        f"· {rec.get('class')}/{rec.get('importance')} · "
        f"_{rec.get('_tier')}_ · "
        f"primary=`{rec.get('primary_decl')}`",
        "",
    ]
    notes = (rec.get("notes") or "").strip()
    if notes:
        lines.append(f"> {notes}")
        lines.append("")
    lines.append("| Mark | Decl | Conf | Source |")
    lines.append("|---|---|---|---|")
    for d in decls:
        link = mathlib_link(d)
        decl_name = d.get("decl", "")
        conf = d.get("confidence", "")
        if link:
            src = f"[{d.get('module','?').rsplit('.', 1)[-1]}]({link})"
        else:
            src = d.get("module", "?")
        lines.append(f"| `_` | `{decl_name}` | {conf} | {src} |")
    lines.append("")
    lines.append(
        "**Article-level verdict** (does Mathlib formalize this concept?): `_`  "
        "_(Y=fully · P=partial · N=no · ?=unclear)_"
    )
    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def render_no_match(rec: dict, idx: int) -> str:
    reason = rec.get("no_match_reason") or "(unspecified)"
    notes = (rec.get("notes") or "").strip()
    lines = [
        f"### {idx}. [{rec['title']}]({wiki_link(rec['title'])}) "
        f"· {rec.get('class')}/{rec.get('importance')} · "
        f"_{rec.get('_tier')}_ · NO MATCH (reason: _{reason}_)",
        "",
    ]
    if notes:
        lines.append(f"> {notes}")
        lines.append("")
    lines.append(
        "**Do you agree there's no Mathlib formalization?** `_`  "
        "_(Y=agree · N=disagree, formalization exists · ?=unclear)_"
    )
    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--pilot-matched", type=int, default=10)
    ap.add_argument("--pilot-nomatch", type=int, default=5)
    ap.add_argument("--tier2-matched", type=int, default=15)
    ap.add_argument("--tier2-nomatch", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20260520)
    args = ap.parse_args()

    pilot = load(PILOT, "pilot")
    tier2 = load(TIER2, "tier2")

    rng = random.Random(args.seed)
    sel: list[dict] = []
    sel += sample([r for r in pilot if (r.get("mathlib_decls") or [])], args.pilot_matched, rng)
    sel += sample([r for r in pilot if not (r.get("mathlib_decls") or []) and not r.get("error")],
                  args.pilot_nomatch, rng)
    sel += sample([r for r in tier2 if (r.get("mathlib_decls") or [])], args.tier2_matched, rng)
    sel += sample([r for r in tier2 if not (r.get("mathlib_decls") or []) and not r.get("error")],
                  args.tier2_nomatch, rng)
    rng.shuffle(sel)  # interleave so the reviewer doesn't bucket by tier

    body = []
    body.append("# WikiLean validation pass — hand-check\n")
    body.append(
        "Replace each `\\_` with a mark. **Use exactly one character per cell, "
        "case-insensitive.**\n"
    )
    body.append("- Per-decl marks (in the table rows):\n"
                "  - `Y` — correct match (this decl formalizes the article's concept)\n"
                "  - `P` — partial (right area, but too narrow/broad or wrong sense)\n"
                "  - `N` — wrong (does not formalize this concept)\n"
                "  - `?` — can't tell\n")
    body.append("- Per-article verdicts (matched articles):\n"
                "  - `Y` Mathlib fully formalizes  ·  `P` partial coverage  ·  "
                "`N` not formalized  ·  `?` unclear\n")
    body.append("- No-match articles: `Y` = you agree there's no formalization; "
                "`N` = you found one; `?` = unclear.\n")
    body.append(f"\nSample: {args.pilot_matched} pilot-matched, "
                f"{args.pilot_nomatch} pilot-no-match, "
                f"{args.tier2_matched} tier2-matched, "
                f"{args.tier2_nomatch} tier2-no-match  "
                f"(seed {args.seed}). Total: **{len(sel)}**.\n")
    body.append("\n---\n")

    matched_n = 0
    nomatch_n = 0
    for i, r in enumerate(sel, 1):
        if r.get("mathlib_decls"):
            body.append(render_matched(r, i))
            matched_n += 1
        else:
            body.append(render_no_match(r, i))
            nomatch_n += 1
        body.append("")

    Path(args.out).write_text("\n".join(body), encoding="utf-8")
    print(f"wrote {args.out}  ({len(sel)} articles: {matched_n} matched, {nomatch_n} no-match)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
