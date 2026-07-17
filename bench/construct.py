"""The ONE code construction shared by gold census and candidate grading.

Divergent assemblies produced a false alarm (fresh_039) and hid a latent bug
(renaming the FIRST decl breaks rows that bundle an auxiliary def). Every
census and every scorer must go through these two functions.
"""
from __future__ import annotations

import re

# theorem/lemma only: auxiliary `def`s in a gold block keep their names (the
# statement references them); the MAIN decl is always the last theorem/lemma.
_DECL = re.compile(
    r"(^|\n)(\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:private\s+|protected\s+|noncomputable\s+)*(?:theorem|lemma)\s+)"
    r"([A-Za-z_][\w.']*)")


def rename_last_decl(code: str, to: str) -> str:
    ms = list(_DECL.finditer(code))
    if not ms:
        return code
    m = ms[-1]
    return code[: m.start(3)] + to + code[m.end(3):]


def _ctx_text(ctx) -> str:
    return "\n".join(str(x) for x in ctx) if isinstance(ctx, list) else (ctx or "")


def assemble_gold(row: dict, name: str = "__gold__") -> str:
    """Gold statement, elaboration-ready. The rename is mandatory on the fresh
    pin — that env CONTAINS the gold theorems ('has already been declared')."""
    parts = (row.get("gold_header") or "", _ctx_text(row.get("gold_context")),
             rename_last_decl(row["gold_formal"], name))
    return "\n".join(p for p in parts if p)


def prepare_candidate(output_lean: str, header: str = "") -> str:
    """Agent output, elaboration-ready — renamed so it can never collide with an
    existing Mathlib name (including the very theorem it reproduces)."""
    body = rename_last_decl(output_lean, "__cand__")
    return (header + "\n" + body) if header else body
