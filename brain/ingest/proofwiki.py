"""ProofWiki ingest adapter — full content via the nightly XML dump.

CLI: python3 brain/ingest/proofwiki.py [--max-age-hours N]

Source: https://proofwiki.org/xmldump/latest.xml.gz (36 MB, regenerated
nightly; robots ai-train=no — reference use only, snippets CC-BY-SA-3.0).
Two streaming passes with xml.etree.iterparse keep memory flat.

Scope: ns 0 (theorems) + ns 100 (Axiom) + ns 102 (Definition) — Definition:/
Axiom: are REAL namespaces in this dump, not title prefixes, so "ns=0 only"
would drop every P6781 Definition value. Page id = title with underscores
(the exact P6781 value format), so `xref:proofwiki:<id>` edge dsts equal our
node ids. #REDIRECT pages become aliases. ns-0 subpages (X/Proof_2,
X/Examples, ...) collapse into the nearest existing ancestor page; Definition:/
Axiom: subpages stay their own pages (P6781 targets them, e.g.
Definition:Coloring/Edge_Coloring).
"""
from __future__ import annotations

import argparse
import gzip
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

DUMP_URL = "https://proofwiki.org/xmldump/latest.xml.gz"
XNS = "{http://www.mediawiki.org/xml/export-0.11/}"
KEEP_NS = {"0", "100", "102"}

# every namespace prefix in the dump's siteinfo — link targets carrying one of
# these (other than Definition/Axiom) are out of scope
SKIP_PREFIXES = {
    "media", "special", "talk", "user", "user talk", "proofwiki",
    "proofwiki talk", "file", "file talk", "image", "mediawiki",
    "mediawiki talk", "template", "template talk", "help", "help talk",
    "category", "category talk", "axiom talk", "definition talk", "symbols",
    "symbols talk", "mathematician", "mathematician talk", "book",
    "book talk", "module", "module talk",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")
ANY_LINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
# structural ns-0 subpage components whose merged links count as proof context
PROOFISH_RE = re.compile(r"(?:proof|lemma|corollary)", re.I)
_WS = re.compile(r"\s+")


def ensure_dump(path: Path, max_age_hours: float) -> None:
    if not common.stale(path, max_age_hours):
        return
    tmp = path.with_suffix(".gz.tmp")
    # -R keeps the remote mtime (the dump's generation time) as the cache gate
    out = subprocess.run(
        ["curl", "-sfL", "-R", "--max-time", "600", "-A", common.USER_AGENT,
         "-o", str(tmp), DUMP_URL], capture_output=True)
    if out.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1 << 20:
        tmp.unlink(missing_ok=True)
        if path.exists():
            print(f"[proofwiki] refresh failed ({out.returncode}); reusing cached dump",
                  file=sys.stderr)
            return
        raise RuntimeError(f"dump download failed: {out.stderr[:200]!r}")
    tmp.rename(path)


def norm_title(t: str) -> str:
    """Wikilink target -> underscore id form (MediaWiki first-letter case)."""
    t = _WS.sub(" ", t.replace("_", " ")).strip().lstrip(":").strip()
    if not t:
        return ""
    if ":" in t:
        pre, rest = t.split(":", 1)
        rest = rest.strip()
        if pre.strip().lower() in ("definition", "axiom"):
            t = pre.strip().capitalize() + ":" + rest[:1].upper() + rest[1:]
        else:
            t = pre.strip() + ":" + rest
    else:
        t = t[:1].upper() + t[1:]
    return t.replace(" ", "_")


def out_of_scope(target: str) -> bool:
    if ":" in target:
        pre = target.split(":", 1)[0].replace("_", " ").lower()
        if pre in SKIP_PREFIXES:
            return True
    return False


def unlink(text: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if "|" in inner:
            return inner.rsplit("|", 1)[1]
        return inner.split("#", 1)[0].lstrip(":")
    return ANY_LINK_RE.sub(repl, text)


def clean_wikitext(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"</?(?:onlyinclude|noinclude|includeonly)[^>]*>", "", s)
    for _ in range(4):  # one nesting level per round
        s2 = re.sub(r"\{\{[^{}]*\}\}", "", s)
        if s2 == s:
            break
        s = s2
    s = unlink(s)
    s = s.replace("'''", "").replace("''", "")
    s = re.sub(r"<[^>]+>", "", s)
    lines = [re.sub(r"^[:;*#]+\s*", "", ln.strip()) for ln in s.splitlines()]
    return " ".join(ln for ln in lines if ln).strip()


def iter_pages(dump: Path):
    with gzip.open(dump, "rb") as f:
        for _, el in ET.iterparse(f):
            if el.tag != XNS + "page":
                continue
            ns = el.findtext(XNS + "ns", "")
            if ns in KEEP_NS:
                title = norm_title(el.findtext(XNS + "title", ""))
                redirect = el.find(XNS + "redirect")
                target = norm_title(redirect.get("title", "")) if redirect is not None else None
                yield ns, title, target, el
            el.clear()


def section_body(text: str, name: str) -> str | None:
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = HEADING_RE.match(ln)
        if m and unlink(m.group(2)).strip().lower() == name:
            start = i + 1
        elif m and start is not None:
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:]) if start is not None else None


def extract_snippet(text: str) -> str:
    lead = text.split("\n==", 1)[0]
    for body in (section_body(text, "theorem"), section_body(text, "definition"),
                 section_body(text, "axiom"), lead):
        if body:
            cleaned = clean_wikitext(body)
            if len(cleaned) >= 20:
                return cleaned
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    args = ap.parse_args()

    dump = common.cache_path("proofwiki", "latest.xml.gz")
    ensure_dump(dump, args.max_age_hours)

    # pass 1: titles + redirects only (small), to build resolve/collapse maps
    redirects: dict[str, str] = {}
    base: dict[str, str] = {}  # non-redirect title -> ns
    for ns, title, target, _ in iter_pages(dump):
        if not title or title == "Main_Page":
            continue
        if target:
            redirects.setdefault(title, target)
        else:
            base[title] = ns

    def chase(t: str) -> str:
        seen = {t}
        while t in redirects:
            t = redirects[t]
            if t in seen or len(seen) > 10:
                break
            seen.add(t)
        return t

    collapse: dict[str, str] = {}  # ns-0 subpage -> nearest existing ancestor
    for title, ns in base.items():
        if ns != "0" or "/" not in title:
            continue
        parts = title.split("/")
        for k in range(1, len(parts)):
            root = "/".join(parts[:k])
            if base.get(root) == "0":
                collapse[title] = root
                break

    def resolve(t: str) -> str | None:
        t = chase(t)
        t = collapse.get(t, t)
        return t if (t in base and t not in collapse) else None

    kept = sorted(t for t, ns in base.items() if t not in collapse)
    kept_set = set(kept)

    qmap = common.qid_map("proofwiki")
    page_qid: dict[str, str] = {}
    for ext_id in sorted(qmap):
        canon = resolve(norm_title(ext_id))
        if canon:
            page_qid.setdefault(canon, qmap[ext_id])

    aliases: dict[str, list[str]] = {}
    for alias in sorted(redirects):
        canon = resolve(alias)
        if canon and alias != canon:
            aliases.setdefault(canon, []).append(alias)

    # pass 2: stream text — links (context-tagged) + snippets/kind hints
    links: dict[tuple[str, str], str] = {}  # (src, dst) -> best context
    rank = {"statement": 0, "proof": 1, "body": 2}
    snippets: dict[str, str] = {}
    kinds: dict[str, str] = {}
    n_unresolved = 0
    for ns, title, target, el in iter_pages(dump):
        if target or title not in base or title == "Main_Page":
            continue
        text = el.findtext(f"{XNS}revision/{XNS}text") or ""
        src = collapse.get(title, title)
        merged = src != title
        if not merged:
            if ns == "102":
                kinds[title] = "definition"
            elif ns == "100":
                kinds[title] = "axiom"
            elif section_body(text, "theorem") is not None:
                kinds[title] = "theorem"
            snip = extract_snippet(text)
            if snip:
                snippets[title] = snip
        if merged:
            last = title.rsplit("/", 1)[1]
            merged_ctx = "proof" if PROOFISH_RE.search(last) else "body"
        context = merged_ctx if merged else "body"
        for ln in text.splitlines():
            m = HEADING_RE.match(ln)
            if m and not merged:
                label = unlink(m.group(2)).strip().lower()
                if label.startswith(("theorem", "definition", "axiom")):
                    context = "statement"
                elif "proof" in label:
                    context = "proof"
                else:
                    context = "body"
            for raw in WIKILINK_RE.findall(ln):
                t = norm_title(raw)
                if not t or out_of_scope(t):
                    continue
                dst = resolve(t)
                if dst is None:
                    n_unresolved += 1
                elif dst != src:
                    key = (src, dst)
                    if key not in links or rank[context] < rank[links[key]]:
                        links[key] = context

    pages = []
    for title in kept:
        row: dict = {"db": "proofwiki", "id": title,
                     "title": title.replace("_", " "),
                     "url": "https://proofwiki.org/wiki/"
                            + urllib.parse.quote(title, safe=":/()',!-")}
        if title in snippets:
            row["snippet"] = snippets[title]
        if title in aliases:
            row["aliases"] = aliases[title]
        if title in kinds:
            row["kind_hint"] = kinds[title]
        if title in page_qid:
            row["qid"] = page_qid[title]
        pages.append(row)
    link_rows = [{"db": "proofwiki", "src": s, "dst": d, "context": c}
                 for (s, d), c in sorted(links.items())]
    common.emit("proofwiki", pages, link_rows, {
        "source_pin": datetime.fromtimestamp(
            dump.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "n_redirects": len(redirects), "n_collapsed": len(collapse),
        "n_links_unresolved": n_unresolved, "n_qid_joined": len(page_qid),
    })


if __name__ == "__main__":
    main()
