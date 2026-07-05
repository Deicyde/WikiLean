#!/usr/bin/env python3
"""Harvest mathlib4's human-authored cross-reference attributes.

Scans every .lean file under the mathlib4 checkout (read-only) for the
`Mathlib/Tactic/CrossRefAttribute.lean` attributes — `@[stacks TAG]`,
`@[kerodon TAG]`, `@[wikidata QID]` — and resolves each to the fully-qualified
name of the declaration it annotates. These are the human-reviewed gold links
(every one merged through mathlib review), so they outrank any agent grounding.

Syntax forms handled (all present in the checkout):
  - bare                       @[stacks 0BR2]
  - with comment string        @[stacks 0BR2 "(1)"]
  - inside an attribute list   @[simp, stacks 0023]  /  @[stacks 0014, wikidata Q719395]
  - multi-line attribute blocks (incl. nested doc comments inside
    `to_additive (attr := wikidata Q...) /-- ... -/`)
  - the attribute command      attribute [stacks 00PM] quasiFinite_iff
  - nested attr :=             @[to_dual (attr := stacks 003B) Epi]

The tag is attached to the declaration the block precedes (or to the named
decls of an `attribute [...] names` command). Generated counterparts
(`to_additive` / `to_dual` targets) are NOT harvested — this is a purely
syntactic, deterministic pass; no LLM, no elaboration.

FQ resolution: a `namespace Foo`/`end Foo` stack is tracked per file
(`section` pushes a scope that does NOT contribute to the namespace); the FQ
name is the namespace stack joined with the inline decl name, verified against
the decl-existence oracle. On a miss the overlap-deduped join, the bare inline
name and shorter namespace-prefix joins are tried; rows that never hit the
oracle keep the plain join and carry "unverified": true (counted in _meta —
the oracle snapshot is known-incomplete, see mathlib_decl_oracle_incomplete).

Output: catalog/data/mathlib_tag_xrefs.jsonl —
  {"_meta": {source, license, harvested_from, counts, ...}}
  {"decl": FQ, "db": "stacks"|"kerodon"|"wikidata", "tag": "0BR2", "file": relpath, "line": n}

Usage:
    python3 catalog/harvest_mathlib_tags.py
    BRAIN_MATHLIB_CHECKOUT=/path/to/mathlib4/Mathlib python3 catalog/harvest_mathlib_tags.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from bisect import bisect_right
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "data" / "mathlib_tag_xrefs.jsonl"
ORACLE = ROOT / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"

MATHLIB_DIR = Path(os.environ.get(
    "BRAIN_MATHLIB_CHECKOUT", "/Users/jack/Desktop/LEAN/mathlib4/Mathlib"))

# stacks/kerodon tags are 4-char [0-9A-Z] Gerby ids; wikidata ids are QIDs.
TAG_RE = re.compile(r"\b(stacks|kerodon)\s+([0-9A-Z]{4})(?![0-9A-Za-z])")
QID_RE = re.compile(r"\bwikidata\s+(Q\d+)\b")

# Lean idents are Unicode (δ, ₁₂, ₗᵢ, ...): [^\W\d] = any word char bar digits.
IDENT = re.compile(r"[^\W\d][\w'!?.«»]*")
DECL_KW = {"theorem", "lemma", "def", "abbrev", "structure", "class",
           "instance", "inductive", "opaque", "axiom",
           "proof_wanted", "irreducible_def"}
MODIFIERS = {"private", "protected", "noncomputable", "unsafe", "partial",
             "nonrec", "scoped", "local", "public", "meta"}

NS_RE = re.compile(r"^[ \t]*namespace[ \t]+(\S+)")
SEC_RE = re.compile(r"^[ \t]*(?:noncomputable[ \t]+)?section\b(?:[ \t]+(\S+))?")
MUT_RE = re.compile(r"^[ \t]*mutual\b")
END_RE = re.compile(r"^[ \t]*end\b(?:[ \t]+(\S+))?")
ATTR_CMD_RE = re.compile(r"^[ \t]*(?:local\s+|scoped\s+)?attribute[ \t]*\[", re.M)


def strip_noise(text: str) -> str:
    """Blank out comments (nested /- -/, line --) and string-literal contents,
    preserving every offset and newline, so the tag/decl/namespace scans never
    fire inside docstrings or attribute comment strings."""
    out = list(text)
    i, n, depth = 0, len(text), 0

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if depth:
            if text.startswith("/-", i):
                depth += 1
                blank(i, i + 2); i += 2
            elif text.startswith("-/", i):
                depth -= 1
                blank(i, i + 2); i += 2
            else:
                blank(i, i + 1); i += 1
            continue
        if text.startswith("/-", i):
            depth = 1
            blank(i, i + 2); i += 2
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            blank(i, j); i = j
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            blank(i, j + 1); i = j + 1
        else:
            i += 1
    return "".join(out)


def match_bracket(text: str, lb: int) -> int:
    """Index just past the ] matching the [ at lb (text is comment/string-stripped)."""
    depth = 0
    for i in range(lb, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def namespace_checkpoints(text: str) -> tuple[list[int], list[list[str]]]:
    """(offsets, namespace-parts) checkpoints from the namespace/section/end
    stack — `section`/`mutual` push scopes that do NOT extend the namespace.
    Scopes are stored one ATOMIC component per entry because Lean permits
    partial closes: `namespace Algebra.FormallySmooth ... end FormallySmooth`
    leaves `Algebra` open, and `end A.B` closes two components at once."""
    stack: list[tuple[str, str | None]] = []    # (kind, component)
    offs, parts = [0], [[]]
    pos = 0
    for line in text.splitlines(keepends=True):
        if m := NS_RE.match(line):
            name = m.group(1)
            if name.startswith("_root_."):
                stack.append(("ns_root", None))
                name = name[len("_root_."):]
            stack.extend(("ns", c) for c in name.split("."))
        elif m := END_RE.match(line):
            for _ in range(len(m.group(1).split(".")) if m.group(1) else 1):
                if stack:
                    stack.pop()
                if stack and stack[-1][0] == "ns_root":
                    stack.pop()                 # marker rides its first component
        elif (m := SEC_RE.match(line)) or MUT_RE.match(line):
            name = m.group(1) if m else None    # m = the section match or None
            stack.extend(("scope", c) for c in name.split(".")) if name \
                else stack.append(("scope", None))
        else:
            pos += len(line)
            continue
        cur: list[str] = []
        for kind, comp in stack:
            if kind == "ns_root":
                cur = []
            elif kind == "ns":
                cur.append(comp)
        offs.append(pos + len(line))
        parts.append(cur)
        pos += len(line)
    return offs, parts


def decl_after(text: str, pos: int) -> tuple[str | None, str | None]:
    """(keyword, inline name) of the declaration following an @[...] block;
    (kw, None) for anonymous instances, (None, None) if no decl keyword found."""
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if text.startswith("@[", pos):           # a further attribute block
            pos = match_bracket(text, pos + 1)
            continue
        m = IDENT.match(text, pos)
        if not m:
            return None, None
        tok = m.group(0)
        if tok in MODIFIERS:
            pos = m.end()
            continue
        if tok not in DECL_KW:
            return None, None
        kw, pos = tok, m.end()
        if kw == "class":                        # class inductive / class abbrev
            while pos < n and text[pos].isspace():
                pos += 1
            m2 = IDENT.match(text, pos)
            if m2 and m2.group(0) in ("inductive", "abbrev"):
                kw, pos = f"class {m2.group(0)}", m2.end()
        while pos < n and text[pos].isspace():
            pos += 1
        if kw == "instance" and pos < n and text[pos] == "(":   # (priority := ...)
            depth = 0
            while pos < n:
                depth += text[pos] == "("
                depth -= text[pos] == ")"
                pos += 1
                if depth == 0:
                    break
            while pos < n and text[pos].isspace():
                pos += 1
        m3 = IDENT.match(text, pos)
        if not m3:
            return kw, None                      # anonymous instance
        return kw, m3.group(0).rstrip(".")       # rstrip: universe `foo.{u}`
    return None, None


def resolve(ns_parts: list[str], inline: str, oracle: set[str]) -> tuple[str, bool]:
    """FQ name candidates, oracle-gated: plain namespace join, overlap-deduped
    join, bare inline name, then shorter namespace-prefix joins."""
    if inline.startswith("_root_."):
        fq = inline[len("_root_."):]
        return fq, fq in oracle
    ip = inline.split(".")
    cands = [".".join(ns_parts + ip)]
    for k in range(min(len(ns_parts), len(ip)), 0, -1):     # ns tail == inline head
        if ns_parts[-k:] == ip[:k]:
            cands.append(".".join(ns_parts + ip[k:]))
            break
    cands.append(inline)
    for j in range(len(ns_parts) - 1, 0, -1):
        cands.append(".".join(ns_parts[:j] + ip))
    seen: set[str] = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        if c in oracle:
            return c, True
    return cands[0], False


def tags_in(content: str) -> list[tuple[str, str, int]]:
    """(db, tag, offset-in-content) for every crossref tag in an attribute list."""
    found = [(m.group(1), m.group(2), m.start(1)) for m in TAG_RE.finditer(content)]
    found += [("wikidata", m.group(1), m.start()) for m in QID_RE.finditer(content)]
    return found


def harvest_file(path: Path, rel: str, oracle: set[str],
                 rows: list[dict], problems: list[str]) -> None:
    text = strip_noise(path.read_text(encoding="utf-8"))
    if "stacks" not in text and "kerodon" not in text and "wikidata" not in text:
        return
    offs, parts = namespace_checkpoints(text)

    def ns_at(pos: int) -> list[str]:
        return parts[bisect_right(offs, pos) - 1]

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def emit(db: str, tag: str, pos: int, inline: str | None, why: str) -> None:
        if inline is None:
            problems.append(f"{rel}:{line_of(pos)} [{db} {tag}] unresolved ({why})")
            return
        fq, ok = resolve(ns_at(pos), inline, oracle)
        row = {"decl": fq, "db": db, "tag": tag, "file": rel, "line": line_of(pos)}
        if not ok:
            row["unverified"] = True
            problems.append(f"{rel}:{line_of(pos)} [{db} {tag}] no oracle hit for "
                            f"'{inline}' in namespace {'.'.join(ns_at(pos)) or '(root)'}"
                            f" — kept '{fq}'")
        rows.append(row)

    # ---- attribute COMMANDS: attribute [..., stacks TAG, ...] name1 name2 ----
    cmd_spans: list[tuple[int, int]] = []
    for m in ATTR_CMD_RE.finditer(text):
        lb = text.index("[", m.start())
        rb = match_bracket(text, lb)
        found = tags_in(text[lb:rb])
        if not found:
            continue
        cmd_spans.append((m.start(), rb))
        eol = text.find("\n", rb)
        eol = len(text) if eol == -1 else eol
        names = IDENT.findall(text[rb:eol])
        if not names:
            problems.append(f"{rel}:{line_of(lb)} attribute command carries a "
                            f"crossref tag but names no decls")
        for db, tag, off in found:
            for name in names:
                emit(db, tag, lb + off, name, "attribute command")

    # ---- @[...] blocks before declarations ----------------------------------
    for m in re.finditer(r"@\[", text):
        if any(a <= m.start() < b for a, b in cmd_spans):
            continue                              # inside an attribute command
        rb = match_bracket(text, m.start() + 1)
        found = tags_in(text[m.start():rb])
        if not found:
            continue
        kw, inline = decl_after(text, rb)
        why = "anonymous instance" if kw else "no decl keyword after block"
        for db, tag, off in found:
            emit(db, tag, m.start() + off, inline, why)


def main() -> int:
    if not MATHLIB_DIR.is_dir():
        print(f"FATAL: mathlib checkout missing at {MATHLIB_DIR} "
              f"(BRAIN_MATHLIB_CHECKOUT to override)", file=sys.stderr)
        return 1
    oracle = set(json.loads(ORACLE.read_text())["declarations"])
    checkout = MATHLIB_DIR.parent

    rows: list[dict] = []
    problems: list[str] = []
    files = sorted(MATHLIB_DIR.rglob("*.lean"))
    for path in files:
        rel = str(path.relative_to(checkout))
        harvest_file(path, rel, oracle, rows, problems)

    rows.sort(key=lambda r: (r["file"], r["line"], r["db"], r["tag"], r["decl"]))
    by_db = Counter(r["db"] for r in rows)
    n_unverified = sum(1 for r in rows if r.get("unverified"))
    meta = {
        "source": "mathlib4 checkout @[stacks]/@[kerodon]/@[wikidata] attributes",
        "license": "Apache-2.0",
        "harvested_from": str(MATHLIB_DIR),
        "oracle": str(ORACLE.relative_to(ROOT)),
        "counts": dict(sorted(by_db.items())),
        "unverified_rows": n_unverified,
        "unresolved_dropped": len(problems) - n_unverified,
        "note": "rows with unverified:true missed the (known-incomplete) decl "
                "oracle under every namespace join and keep the plain join; "
                "generated to_additive/to_dual counterparts are not harvested",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(OUT)

    print(f"harvested {len(rows)} tag rows from {len(files)} files -> "
          f"{OUT.relative_to(ROOT)}")
    for db, c in sorted(by_db.items()):
        print(f"  {db:9s} {c}")
    print(f"  unverified (kept, no oracle hit): {n_unverified}")
    print(f"  unresolved (dropped, no decl):    {len(problems) - n_unverified}")
    for p in problems:
        print(f"    - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
