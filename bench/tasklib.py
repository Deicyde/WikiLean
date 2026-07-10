#!/usr/bin/env python3
"""Shared plumbing for the Wikibrain benchmark (bench/ — docs/BRAIN-V2.md axis 5).

Task file contract (bench/data/tasks.jsonl, first line {"_meta": ...}):
  T1 informal->formal   {id, type:"T1", split, prompt_context:{label, slug, statement?},
                         gold:{decl, decls[], qid}, provenance}
  T2 formal->informal   {id, type:"T2", split, prompt_context:{decl, module?},
                         gold:{pairs:[{qid, slug, slugs[], label}]}, provenance}
  T3 formalized-or-not  {id, type:"T3", split, prompt_context:{label, slug, statement?},
                         gold:{formalized, witness_decl?, witness_decls[]}, provenance}

The runner and scorer share the prompt builder and the STRICT-final-line answer
parser so the wire format can never drift between them.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "bench"
DATA_DIR = BENCH / "data"
TASKS_PATH = DATA_DIR / "tasks.jsonl"

# Last ANSWER: line wins (models sometimes restate the format before answering).
ANSWER_RE = re.compile(r"^\s*ANSWER:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_QID_RE = re.compile(r"\bQ\d+\b")
_T3_RE = re.compile(r"^(YES|NO)\b[\s:,.\-]*(.*)$", re.IGNORECASE | re.DOTALL)
# T2 filler words models wrap around "<QID> <slug>" despite the strict format.
_T2_NOISE = {"qid", "slug", "wikipedia", "article", "enwiki", "wikidata", "the", "-", "|", "/"}


def load_tasks(path: Path = TASKS_PATH) -> list[dict]:
    tasks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "_meta" in r:
                continue
            tasks.append(r)
    return tasks


def write_jsonl(path: Path, meta: dict, rows: list[dict]) -> None:
    """Atomic jsonl write, first line _meta (same convention as brain/ingest)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Prompts — IDENTICAL across arms (the arms differ only in tool availability).
# ---------------------------------------------------------------------------

def build_prompt(task: dict) -> str:
    t = task["type"]
    ctx = task["prompt_context"]
    if t == "T1":
        lines = [
            "You are auditing formalization coverage of mathematics in Mathlib4 "
            "(the Lean 4 mathematical library, leanprover-community/mathlib4).",
            "",
            f"Concept: {ctx['label']}",
            f"English Wikipedia article: {ctx['slug']}",
        ]
        if ctx.get("statement"):
            lines.append(f"Statement: {ctx['statement']}")
        lines += [
            "",
            "Question: what is the fully-qualified name of the single Mathlib4 "
            "declaration that best formalizes this concept/statement?",
            "",
            "Your reply MUST end with exactly one line of the form:",
            "ANSWER: <Fully.Qualified.DeclName>",
        ]
        return "\n".join(lines)
    if t == "T2":
        lines = [
            "You are identifying the informal mathematical concept behind a "
            "Mathlib4 (Lean 4) declaration.",
            "",
            f"Mathlib4 declaration: {ctx['decl']}",
        ]
        if ctx.get("module"):
            lines.append(f"Module: {ctx['module']}")
        lines += [
            "",
            "Question: which mathematical concept does this declaration formalize? "
            "Give the Wikidata QID and the English Wikipedia article slug "
            "(the article title with underscores).",
            "",
            "Your reply MUST end with exactly one line of the form:",
            "ANSWER: <QID> <Wikipedia_article_slug>",
        ]
        return "\n".join(lines)
    if t == "T3":
        lines = [
            "You are auditing formalization coverage of mathematics in Mathlib4 "
            "(the Lean 4 mathematical library, leanprover-community/mathlib4).",
            "",
            f"Concept: {ctx['label']}",
            f"English Wikipedia article: {ctx['slug']}",
        ]
        if ctx.get("statement"):
            lines.append(f"Statement: {ctx['statement']}")
        else:
            lines.append("Scope: the concept itself, as a definition or theorem.")
        lines += [
            "",
            "Question: is this formalized in Mathlib4? If YES, name one witness "
            "declaration (fully-qualified).",
            "",
            "Your reply MUST end with exactly one line, either:",
            "ANSWER: YES <Fully.Qualified.WitnessDeclName>",
            "or:",
            "ANSWER: NO",
        ]
        return "\n".join(lines)
    raise ValueError(f"unknown task type {t!r}")


# ---------------------------------------------------------------------------
# Answer parsing — mechanical, no judgment calls.
# ---------------------------------------------------------------------------

def _clean_decl(tok: str) -> str | None:
    # Lean decl names legitimately contain '.' and trailing "'" (primes) —
    # strip only markdown/quoting wrappers and sentence punctuation.
    tok = tok.strip().strip("`*").strip('"').rstrip(".,;:!?").strip("`*")
    return tok or None


def parse_answer(task_type: str, raw: str | None) -> dict | None:
    """Extract the final ANSWER: line. Returns None when no ANSWER line exists."""
    hits = ANSWER_RE.findall(raw or "")
    if not hits:
        return None
    ans = hits[-1].strip()
    if task_type == "T1":
        toks = ans.split()
        return {"decl": _clean_decl(toks[0])} if toks else {"decl": None}
    if task_type == "T2":
        m = _QID_RE.search(ans)
        qid = m.group(0) if m else None
        rest = (ans[: m.start()] + " " + ans[m.end():]) if m else ans
        toks = [t.strip("`*\"',;:()[]") for t in rest.split()]
        toks = [t for t in toks if t and t.casefold() not in _T2_NOISE]
        slug = "_".join(toks) if toks else None
        return {"qid": qid, "slug": slug}
    if task_type == "T3":
        m = _T3_RE.match(ans)
        if not m:
            return {"verdict": None, "witness": None}
        verdict = m.group(1).upper()
        witness = None
        tail = m.group(2).strip()
        if verdict == "YES" and tail:
            witness = _clean_decl(tail.split()[0])
        return {"verdict": verdict, "witness": witness}
    raise ValueError(f"unknown task type {task_type!r}")


def sanitize_slug(s: str | None) -> str:
    """Normalize a Wikipedia slug for comparison. WikiLean slugs are sanitized
    (apostrophes/parens/en-dashes stripped: Group_(mathematics) -> Group_mathematics)
    while models answer real enwiki slugs — canonicalize both sides."""
    if not s:
        return ""
    s = urllib.parse.unquote(str(s)).strip().strip("`*\"")
    s = s.replace("–", "-").replace("—", "-")
    for ch in "'‘’ʼ\"()":
        s = s.replace(ch, "")
    s = re.sub(r"[\s/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.casefold()
