"""nLab ingest adapter — full content via the ncatlab/nlab-content git mirror.

CLI: python3 brain/ingest/nlab.py [--max-age-hours N]

Source: https://github.com/ncatlab/nlab-content (daily push; ~20.7k pages at
pages/<d1>/<d2>/<d3>/<d4>/<pageid>/{name,content.md}, d1..d4 = last four digits
of the page id reversed — we just walk the tree). Page id for the contract is
the page NAME verbatim (spaces included) — the exact P4215 value format in
wikidata_crossrefs.json, so `xref:nlab:<id>` edge dsts equal our node ids.
[[!redirects ...]] aliases are emitted per page so build_common can resolve
P4215 values that point at an alias instead of the canonical name.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

REPO_URL = "https://github.com/ncatlab/nlab-content"

REDIRECT_RE = re.compile(r"\[\[!redirects\s+([^\]]+?)\s*\]\]")
# skips [[!include ...]] / [[!redirects ...]] via (?!!); drops #anchor and |label
WIKILINK_RE = re.compile(r"\[\[(?!!)([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
HEADING_RE = re.compile(r"^\s*#{1,6}\s*\S", re.M)
IDEA_RE = re.compile(r"^\s*#{1,6}\s*Idea\s*#*\s*$", re.M | re.I)
ANY_LINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
_WS = re.compile(r"\s+")


def sync_repo(repo_dir: Path, max_age_hours: float) -> str:
    git = repo_dir / ".git"
    if not git.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", REPO_URL,
                        str(repo_dir)], check=True)
    else:
        marker = git / "FETCH_HEAD"
        if common.stale(marker if marker.exists() else git / "HEAD", max_age_hours):
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth", "1",
                            "--quiet", "origin", "HEAD"], check=True)
            # checkout -B (never reset --hard) per repo convention
            subprocess.run(["git", "-C", str(repo_dir), "checkout", "--quiet",
                            "-B", "ingest", "FETCH_HEAD"], check=True)
    out = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def strip_sidebars(content: str) -> str:
    """Drop +-- ... =-- block (possibly nested) sidebar/context boxes."""
    depth = 0
    kept: list[str] = []
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith("+--"):
            depth += 1
            continue
        if s.startswith("=--"):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            kept.append(ln)
    return "\n".join(kept)


def unlink(text: str) -> str:
    """[[a|b]] -> b, [[a#s]] -> a, [[!directive]] -> ''."""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith("!"):
            return ""
        if "|" in inner:
            return inner.rsplit("|", 1)[1]
        return inner.split("#", 1)[0]
    return ANY_LINK_RE.sub(repl, text)


def _directive_line(s: str) -> bool:
    return (not s or s.startswith(("* table of contents", "{:", "{#", "***", "---", "|"))
            or s == "{: toc}")


def extract_snippet(content: str) -> str:
    text = strip_sidebars(content)
    m = IDEA_RE.search(text)
    if m:
        body = text[m.end():]
        nxt = HEADING_RE.search(body)
        if nxt:
            body = body[: nxt.start()]
        para = _first_para(body)
        if para:
            return para
    # fallback: first non-directive paragraph of the whole page
    return _first_para(text)


def _first_para(body: str) -> str:
    for chunk in re.split(r"\n\s*\n", body):
        lines = [ln.strip() for ln in chunk.splitlines()]
        lines = [ln for ln in lines if not _directive_line(ln)]
        if not lines or lines[0].lstrip().startswith("#"):
            continue
        para = unlink(" ".join(lines))
        para = _WS.sub(" ", para).strip()
        # prose only — skip embedded images / svg / giant unbroken tokens
        if "data:image" in para or "<svg" in para or "![" in para:
            continue
        if len(para) >= 20 and max(map(len, para.split())) < 120:
            return para
    return ""


def build(repo_dir: Path) -> tuple[list[dict], list[dict], dict]:
    raw: list[tuple[str, str]] = []  # (name, content)
    n_junk = 0
    for name_file in sorted((repo_dir / "pages").glob("*/*/*/*/*/name")):
        name = name_file.read_text(encoding="utf-8", errors="replace").strip()
        content_file = name_file.parent / "content.md"
        content = (content_file.read_text(encoding="utf-8", errors="replace")
                   if content_file.exists() else "")
        if not name or " > history" in name or not content.strip():
            n_junk += 1
            continue
        raw.append((name, content))
    raw.sort()

    # alias -> canonical name map; a page's own name always wins over redirects
    alias_map: dict[str, str] = {name: name for name, _ in raw}
    for name, content in raw:
        for alias in REDIRECT_RE.findall(content):
            alias = _WS.sub(" ", alias).strip()
            if alias:
                alias_map.setdefault(alias, name)
    cf_map: dict[str, str] = {}
    for alias in sorted(alias_map):
        cf_map.setdefault(alias.casefold(), alias_map[alias])

    def resolve(target: str) -> str | None:
        target = _WS.sub(" ", target).strip()
        return alias_map.get(target) or cf_map.get(target.casefold())

    # P4215 values may name an alias — join them through the redirect map
    qmap = common.qid_map("nlab")
    page_qid: dict[str, str] = {}
    for ext_id in sorted(qmap):
        canon = resolve(ext_id)
        if canon:
            page_qid.setdefault(canon, qmap[ext_id])

    page_aliases: dict[str, list[str]] = {}
    for alias in sorted(alias_map):
        canon = alias_map[alias]
        if alias != canon:
            page_aliases.setdefault(canon, []).append(alias)

    pages: list[dict] = []
    link_set: set[tuple[str, str]] = set()
    n_unresolved = 0
    for name, content in raw:
        row: dict = {
            "db": "nlab", "id": name, "title": name,
            "url": "https://ncatlab.org/nlab/show/"
                   + urllib.parse.quote(name.replace(" ", "+"), safe="+"),
        }
        snippet = extract_snippet(content)
        if snippet:
            row["snippet"] = snippet
        if name in page_aliases:
            row["aliases"] = page_aliases[name]
        if name in page_qid:
            row["qid"] = page_qid[name]
        pages.append(row)
        for target in WIKILINK_RE.findall(content):
            canon = resolve(target)
            if canon is None:
                n_unresolved += 1
            elif canon != name:
                link_set.add((name, canon))
    links = [{"db": "nlab", "src": s, "dst": d, "context": "body"}
             for s, d in sorted(link_set)]
    stats = {"n_junk_skipped": n_junk, "n_aliases": len(alias_map) - len(raw),
             "n_links_unresolved": n_unresolved,
             "n_qid_joined": len(page_qid)}
    return pages, links, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    args = ap.parse_args()

    repo_dir = common.cache_path("nlab", "nlab-content")
    head = sync_repo(repo_dir, args.max_age_hours)
    pages, links, stats = build(repo_dir)
    common.emit("nlab", pages, links, {"source_pin": head, **stats})


if __name__ == "__main__":
    main()
