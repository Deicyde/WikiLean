#!/usr/bin/env python3
"""Stacks Project ingest (FULL) — git mirror, tags/tags + chapter .tex parse.

Shallow-clones github.com/stacks/stacks-project into the external cache, maps
every tag (tags/tags: `tag,full_label`) to a page, extracts statement snippets
from theorem-like environments (GFDL permits storage), and turns \\ref{} cross
references into links with context statement|proof. No Wikidata property exists
for Stacks tags, so `qid` is never set (decl-level joins ride @[stacks] xrefs).

Run: python3 brain/ingest/stacks.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

GIT_URL = "https://github.com/stacks/stacks-project"
TAG_URL = "https://stacks.math.columbia.edu/tag/{}"
CLONE = common.cache_path("stacks", "stacks-project")

# theorem-like environments whose body is a statement (snippet-bearing)
STATEMENT_ENVS = {
    "theorem", "proposition", "lemma", "definition", "remark", "remarks",
    "example", "exercise", "situation",
}
# sub-environments stripped from statement bodies before snippeting
STRIP_ENVS = ("reference", "slogan", "history")
# recognized kind tokens (first label token after the chapter prefix)
KIND_TOKENS = STATEMENT_ENVS | {
    "section", "subsection", "subsubsection", "equation", "item", "chapter", "part",
}

TOKEN = re.compile(
    r"\\(begin|end)\{([a-zA-Z*]+)\}|\\(label|ref)\{([^}]+)\}")
COMMENT = re.compile(r"(?<!\\)%.*")
CITE = re.compile(r"\\cite(?:\[[^\]]*\])?\{[^}]*\}")
LABEL_CMD = re.compile(r"\\label\{[^}]*\}")


def refresh_clone() -> str:
    """Clone or fast-forward the cache checkout; return the HEAD sha (pin)."""
    if not (CLONE / ".git").exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", GIT_URL, str(CLONE)], check=True)
    elif common.stale(CLONE / ".git" / "FETCH_HEAD", 20):
        try:
            subprocess.run(["git", "-C", str(CLONE), "fetch", "--depth", "1",
                            "origin", "master"], check=True, timeout=600)
            subprocess.run(["git", "-C", str(CLONE), "checkout", "-B", "master",
                            "FETCH_HEAD"], check=True, capture_output=True)
        except Exception as e:  # noqa: BLE001 — fail-soft: parse the old checkout
            print(f"[stacks] fetch failed ({e}); using existing checkout",
                  file=sys.stderr)
    return subprocess.run(["git", "-C", str(CLONE), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def load_tags() -> dict[str, str]:
    """full_label -> TAG from tags/tags (CSV with '#' comments)."""
    out: dict[str, str] = {}
    for line in (CLONE / "tags" / "tags").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag, _, full_label = line.partition(",")
        out[full_label] = tag
    return out


def split_chapter(full_label: str, stems: set[str]) -> tuple[str, str]:
    """(chapter, rest) by longest chapter-stem prefix; ('', label) if none."""
    parts = full_label.split("-")
    for i in range(len(parts) - 1, 0, -1):
        cand = "-".join(parts[:i])
        if cand in stems:
            return cand, "-".join(parts[i:])
    return "", full_label


def kind_hint(full_label: str, stems: set[str]) -> str | None:
    _, rest = split_chapter(full_label, stems)
    token = rest.split("-", 1)[0]
    return token if token in KIND_TOKENS else None


def clean_body(body: str) -> str:
    for env in STRIP_ENVS:
        body = re.sub(
            r"\\begin\{" + env + r"\}.*?\\end\{" + env + r"\}", " ", body, flags=re.S)
    body = LABEL_CMD.sub(" ", body)
    body = CITE.sub(" ", body)
    return body


def parse_chapter(stem: str, text: str, stems: set[str],
                  snippets: dict[str, str], refs: list[tuple[str, str, str]]) -> None:
    """Fill snippets[full_label] = statement body and refs (src_full, dst_full, ctx)."""
    text = COMMENT.sub("", text)
    stack: list[list] = []  # [env, body_start, label|None]
    last_statement_label: str | None = None
    proof_depth = 0
    proof_src: str | None = None
    for m in TOKEN.finditer(text):
        be, env, cmd, arg = m.group(1), m.group(2), m.group(3), m.group(4)
        if be == "begin":
            if env == "proof":
                proof_depth += 1
                proof_src = last_statement_label
            else:
                stack.append([env, m.end(), None])
        elif be == "end":
            if env == "proof":
                proof_depth = max(0, proof_depth - 1)
                if proof_depth == 0:
                    proof_src = None
            else:
                while stack:  # tolerate mismatched \end
                    top = stack.pop()
                    if top[0] == env:
                        if env in STATEMENT_ENVS:
                            last_statement_label = top[2]
                            if top[2] is not None:
                                snippets[f"{stem}-{top[2]}"] = clean_body(
                                    text[top[1]:m.start()])
                        break
        elif cmd == "label":
            # a label belongs to the innermost open env; only statement envs
            # anchor snippets/refs (equation/item labels are sub-locations)
            if stack and stack[-1][0] in STATEMENT_ENVS and stack[-1][2] is None:
                stack[-1][2] = arg
        elif cmd == "ref":
            if proof_depth and proof_src is not None:
                src_full, ctx = f"{stem}-{proof_src}", "proof"
            else:
                owner = next((s[2] for s in reversed(stack)
                              if s[0] in STATEMENT_ENVS and s[2] is not None), None)
                if owner is None:
                    continue  # ref in running text — no tagged anchor
                src_full, ctx = f"{stem}-{owner}", "statement"
            chapter, _ = split_chapter(arg, stems)
            dst_full = arg if chapter else f"{stem}-{arg}"
            refs.append((src_full, dst_full, ctx))


def main() -> int:
    pin = refresh_clone()
    tags = load_tags()
    stems = {p.stem for p in CLONE.glob("*.tex")}
    snippets: dict[str, str] = {}
    refs: list[tuple[str, str, str]] = []
    for tex in sorted(CLONE.glob("*.tex")):
        parse_chapter(tex.stem, tex.read_text(errors="replace"), stems,
                      snippets, refs)

    pages = []
    for full_label, tag in sorted(tags.items(), key=lambda kv: kv[1]):
        row = {"db": "stacks", "id": tag, "title": full_label,
               "url": TAG_URL.format(tag)}
        kh = kind_hint(full_label, stems)
        if kh:
            row["kind_hint"] = kh
        snip = snippets.get(full_label)
        if snip and snip.strip():
            row["snippet"] = snip
        pages.append(row)

    links, unresolved = set(), 0
    for src_full, dst_full, ctx in refs:
        src, dst = tags.get(src_full), tags.get(dst_full)
        if src is None or dst is None:
            unresolved += 1
            continue
        links.add((src, dst, ctx))
    link_rows = [{"db": "stacks", "src": s, "dst": d, "context": c}
                 for s, d, c in sorted(links)]

    common.emit("stacks", pages, link_rows, extra_meta={
        "source_pin": pin,
        "n_snippets": sum(1 for p in pages if "snippet" in p),
        "n_refs_raw": len(refs),
        "n_refs_unresolved": unresolved,
        "n_chapters": len(stems),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
