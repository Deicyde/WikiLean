#!/usr/bin/env python3
"""formal-conjectures ingest (google-deepmind/formal-conjectures, Apache-2.0).

Harvests EVERY declaration of the FormalConjectures/ library from a local
git checkout (cloned/pulled here — never inside WikiLean): fully-qualified
name, module, declaration kind, the @[category ..., AMS ...] attribute, the
/-- docstring -/ (the informal statement — Apache-2.0, storable with
attribution), a statement-header code snippet, and every reference the
docstrings cite (erdosproblems.com ids, Wikipedia slugs, OEIS A-numbers).

Output: catalog/data/formal_conjectures.jsonl (committed; _meta first line).
Deterministic, no LLM — fuzzy concept joins are the agent fleet's job via
brain/proposals/ fc_link rows + fold_proposals.py. build_common's
formal-conjectures layer mints decl:FormalConjectures:* nodes,
path:FormalConjectures/* containers and decl->xref:erdos/oeis edges from
this file (fail-soft: file missing = layer skipped).

Run: python3 brain/ingest/formal_conjectures.py
Env: BRAIN_FC_CHECKOUT (default /Users/jack/Desktop/LEAN/formal-conjectures-mirror)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common                     # noqa: E402  (brain/ingest/common.py)
import build_common               # noqa: E402  (brain/build_common.py — Lean parser)

REPO_URL = "https://github.com/google-deepmind/formal-conjectures.git"
CHECKOUT = Path(os.environ.get(
    "BRAIN_FC_CHECKOUT", "/Users/jack/Desktop/LEAN/formal-conjectures-mirror"))
OUT = common.REPO / "catalog" / "data" / "formal_conjectures.jsonl"

CATEGORY = re.compile(r"@\[\s*category\s+([^,\]]+?)\s*(?:,\s*AMS\s+([\d\s]+))?\s*\]")
ERDOS_URL = re.compile(r"erdosproblems\.com/(\d+(?:-\d+)*)")
WIKI_URL = re.compile(r"en\.wikipedia\.org/wiki/([^\s\)\]\},\"'>]+)")
OEIS_URL = re.compile(r"oeis\.org/(A\d{6})")
DOCSTRING_MAX = 700
CODE_MAX = 700
_KW = re.compile(r"\b(theorem|lemma|def|abbrev|structure|class|inductive"
                 r"|instance|opaque|axiom)\s")


def ensure_checkout() -> str:
    """Clone or (at most daily) ff-pull the mirror; return the commit pin.
    Fail-soft on network: an existing checkout is always usable as-is."""
    if not (CHECKOUT / ".git").exists():
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(CHECKOUT)],
                       check=True)
    else:
        stamp = CHECKOUT / ".git" / "FETCH_HEAD"
        if not stamp.exists() or time.time() - stamp.stat().st_mtime > 20 * 3600:
            r = subprocess.run(["git", "-C", str(CHECKOUT), "pull", "--ff-only"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[formal_conjectures] pull failed (using existing checkout): "
                      f"{r.stderr.strip()[:200]}", file=sys.stderr)
    out = subprocess.run(["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def _clean_slug(raw: str) -> str:
    """Wikipedia slug from a matched URL tail: strip fragment/query and the
    trailing punctuation that markdown links leave behind; percent-decode."""
    s = raw.split("#", 1)[0].split("?", 1)[0]
    s = s.rstrip(".,;:!\"'*_")
    while s.endswith(")") and s.count("(") < s.count(")"):
        s = s[:-1]
    return urllib.parse.unquote(s)


def _refs(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, rx, norm in (("erdos", ERDOS_URL, str),
                          ("wikipedia", WIKI_URL, _clean_slug),
                          ("oeis", OEIS_URL, str)):
        vals: list[str] = []
        for m in rx.findall(text or ""):
            v = norm(m)
            if v and v not in vals:
                vals.append(v)
        if vals:
            out[key] = vals
    return out


def _module_docstring(lines: list[str]) -> str:
    """Text of the first /-! ... -/ block (the file header), '' when absent."""
    text: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            i = line.find("/-!")
            if i >= 0:
                inside = True
                line = line[i + 3:]
            else:
                continue
        j = line.find("-/")
        if j >= 0:
            text.append(line[:j])
            break
        text.append(line)
    return "\n".join(text).strip()


def _decl_context(lines: list[str], idx: int) -> tuple[str, str]:
    """(attribute text, docstring text) attached to the decl at line idx.

    Walks upward from the declaration: the attribute block is the contiguous
    @[...] lines directly above (or inline on the decl line itself); the
    docstring is the /-- ... -/ block directly above that. Anything else
    (blank line excepted) ends the walk — a stray earlier docstring must not
    attach to the wrong declaration.
    """
    attr = ""
    m = re.match(r"\s*(@\[[^\]]*\])", lines[idx])
    if m:
        attr = m.group(1)
    i = idx - 1
    # contiguous attribute lines above (possibly a multi-line @[ ... ] block)
    while i >= 0:
        s = lines[i].strip()
        if not s:
            break
        if s.startswith("@[") or (attr == "" and s.endswith("]") and "@[" in s):
            attr = s + " " + attr
            i -= 1
            continue
        break
    # docstring block ending directly above
    if i >= 0 and lines[i].strip().endswith("-/"):
        end = i
        while i >= 0 and "/--" not in lines[i]:
            i -= 1
        if i >= 0:
            block = "\n".join(lines[i:end + 1])
            block = block[block.find("/--") + 3:]
            j = block.rfind("-/")
            if j >= 0:
                block = block[:j]
            return attr, block.strip()
    return attr, ""


def _code_snippet(lines: list[str], idx: int) -> str:
    """Statement header from the decl line — the same heuristic as
    build_common's checkout snippets (12 lines, stop at := by / where)."""
    snip: list[str] = []
    for line in lines[idx:idx + 12]:
        s = line.rstrip()
        if snip and not s:
            break
        snip.append(line)
        if (s.endswith(":=") or s.endswith(":= by") or s.endswith(" by")
                or s.endswith("where") or s.endswith(":= fun")
                or s.endswith("sorry")):
            break
    return "\n".join(snip)[:CODE_MAX]


def main() -> int:
    commit = ensure_checkout()
    root = CHECKOUT / "FormalConjectures"
    if not root.is_dir():
        raise RuntimeError(f"checkout has no FormalConjectures/ at {CHECKOUT}")

    rows: list[dict] = []
    n_files = 0
    for fp in sorted(root.rglob("*.lean")):
        rel = fp.relative_to(CHECKOUT).as_posix()
        try:
            lines = fp.read_text().splitlines()
        except OSError as e:
            print(f"[formal_conjectures] unreadable {rel}: {e}", file=sys.stderr)
            continue
        n_files += 1
        module = rel[:-len(".lean")].replace("/", ".")
        header = _module_docstring(lines)
        file_refs = _refs(header)
        declared = build_common._lean_decl_lines(lines)
        for fq in sorted(declared):
            idx = declared[fq]
            attr, doc = _decl_context(lines, idx)
            cm = CATEGORY.search(attr)
            category = cm.group(1).strip() if cm else None
            ams = [int(x) for x in (cm.group(2) or "").split()] if cm else []
            kw = _KW.search(re.sub(r"@\[[^\]]*\]", "", lines[idx]))
            row = {
                "decl": fq, "module": module, "file": rel,
                "kind": kw.group(1) if kw else None,
                "category": category, "ams": ams or None,
                "docstring": common.clean_snippet(doc, DOCSTRING_MAX) or None,
                "code": _code_snippet(lines, idx) or None,
                "refs": _refs(doc) or None,
                "file_refs": file_refs or None,
            }
            rows.append({k: v for k, v in row.items() if v is not None})

    if not rows:
        raise RuntimeError("harvested 0 declarations — refusing to write (fail-soft)")
    common._volume_guard(OUT, "decl", len(rows))
    n_research = sum(1 for r in rows if (r.get("category") or "").startswith("research"))
    common.write_jsonl(OUT, {
        "source": "google-deepmind/formal-conjectures",
        "license": "Apache-2.0 (The Formal Conjectures Authors) — docstrings/code "
                   "stored with attribution",
        "commit": commit,
        "fetched_at": common.now_iso(),
        "n_files": n_files,
        "n_decls": len(rows),
        "n_research": n_research,
    }, rows)
    print(f"[formal_conjectures] wrote {len(rows)} decls ({n_research} research) "
          f"from {n_files} files @ {commit[:12]} -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
