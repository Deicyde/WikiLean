"""PlanetMath ingest adapter — full content via the planetmath/* MSC git repos.

CLI: python3 brain/ingest/planetmath.py [--max-age-hours N]

Source: the ~63 MSC content repos of https://github.com/planetmath (names like
11_Number_theory), one .tex file per entry. Page id = \\pmcanonicalname
VERBATIM (CamelCase) — verified against P7726 values in wikidata_crossrefs.json
(255/258 are mixed-case, e.g. SzemeredisTheorem), so `xref:planetmath:<id>`
edge dsts equal our node ids. planetmath.org URLs are case-insensitive, so
https://planetmath.org/<id> resolves. Edges = \\pmrelated (context 'related');
aliases = \\pmsynonym; kind_hint = \\pmtype lowercased.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

ORG_REPOS_URL = "https://api.github.com/orgs/planetmath/repos?per_page=100&page={page}"
CONTENT_REPO_RE = re.compile(r"^\d{2}_")
CANONICAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9'\-]*$")
_WS = re.compile(r"\s+")

# \cmd{text} -> text, for text-level commands only (math stays verbatim)
TEXT_CMDS = re.compile(
    r"\\(?:emph|textbf|textit|textrm|texttt|textsl|underline|mbox|text|"
    r"PMlinkescapetext)\s*\{([^{}]*)\}")
LINKNAME = re.compile(r"\\PMlink(?:name|id)\s*\{([^{}]*)\}\s*\{[^{}]*\}")
ENV = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}(?:\s*\[[^\]]*\])?")
COMMENT = re.compile(r"(?<!\\)%.*")
LINK_ESCAPES = re.compile(r"\\PMlinkescape(?:word|phrase)\s*\{[^{}]*\}", re.I)
# TeX accents -> combining marks (composed via NFC), outside math only in practice
ACCENTS = {"'": "́", "`": "̀", "^": "̂", '"': "̈",
           "~": "̃", "=": "̄", ".": "̇",
           "v": "̌", "u": "̆", "H": "̋", "c": "̧"}
ACCENT_RE = re.compile(r"\\(['`^\"~=.])\{?([a-zA-Z])\}?|\\([vuHc])\{([a-zA-Z])\}")
LIGATURES = {"\\ss": "ß", "\\ae": "æ", "\\AE": "Æ", "\\aa": "å", "\\AA": "Å",
             "\\o": "ø", "\\O": "Ø", "\\l": "ł", "\\L": "Ł"}


def _accent(m: re.Match) -> str:
    mark, letter = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return unicodedata.normalize("NFC", letter + ACCENTS[mark])


def list_content_repos() -> list[str]:
    """GitHub API listing, cached fail-soft (falls back to cloned dirs)."""
    cache = common.cache_path("planetmath", "repos.json")
    try:
        names: list[str] = []
        page = 1
        while True:
            batch = json.loads(common.curl_fetch(ORG_REPOS_URL.format(page=page)))
            names += [r["name"] for r in batch]
            if len(batch) < 100:
                break
            page += 1
        repos = sorted(n for n in names if CONTENT_REPO_RE.match(n))
        cache.write_text(json.dumps(repos))
        return repos
    except Exception as e:  # noqa: BLE001 — fail-soft to cache/clones
        if cache.exists():
            print(f"[planetmath] repo listing failed ({e}); using cached list",
                  file=sys.stderr)
            return json.loads(cache.read_text())
        cloned = sorted(p.name for p in (common.CACHE_DIR / "planetmath").glob("*")
                        if CONTENT_REPO_RE.match(p.name) and (p / ".git").exists())
        if cloned:
            print(f"[planetmath] repo listing failed ({e}); using {len(cloned)} clones",
                  file=sys.stderr)
            return cloned
        raise


def sync_repo(name: str, max_age_hours: float) -> str:
    repo_dir = common.cache_path("planetmath", name)
    git = repo_dir / ".git"
    if not git.exists():
        subprocess.run(["git", "clone", "--depth", "1", "--quiet",
                        f"https://github.com/planetmath/{name}", str(repo_dir)],
                       check=True)
    else:
        marker = git / "FETCH_HEAD"
        if common.stale(marker if marker.exists() else git / "HEAD", max_age_hours):
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth", "1",
                            "--quiet", "origin", "HEAD"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "checkout", "--quiet",
                            "-B", "ingest", "FETCH_HEAD"], check=True)
    out = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def cmd_args(tex: str, name: str) -> list[str]:
    """All balanced-brace arguments of \\<name>{...} occurrences."""
    out: list[str] = []
    token = "\\" + name
    i = 0
    while (j := tex.find(token, i)) >= 0:
        k = j + len(token)
        if k < len(tex) and (tex[k].isalpha()):  # longer command name, e.g. \pmtypo
            i = k
            continue
        while k < len(tex) and tex[k].isspace():
            k += 1
        if k >= len(tex) or tex[k] != "{":
            i = j + len(token)
            continue
        depth = 0
        for m in range(k, len(tex)):
            if tex[m] == "{":
                depth += 1
            elif tex[m] == "}":
                depth -= 1
                if depth == 0:
                    out.append(tex[k + 1:m])
                    i = m + 1
                    break
        else:
            break
    return out


def detex(s: str) -> str:
    s = COMMENT.sub("", s)
    s = LINK_ESCAPES.sub("", s)
    s = LINKNAME.sub(r"\1", s)
    s = ACCENT_RE.sub(_accent, s)
    for lig, ch in LIGATURES.items():
        s = re.sub(re.escape(lig) + r"(?![a-zA-Z])\{?\}?", ch, s)
    for _ in range(4):  # nested text commands + {\em ...} group switches
        s2 = re.sub(r"\{\\(?:em|bf|it|rm|sl|tt|PMlinkescapetext)\s+([^{}]*)\}", r"\1", s)
        s2 = TEXT_CMDS.sub(r"\1", s2)
        if s2 == s:
            break
        s = s2
    s = ENV.sub(" ", s)
    s = s.replace("\\%", "%").replace("\\&", "&").replace("\\_", "_")
    s = re.sub(r"\\(?:label|ref|eqref|cite|index|url|footnote)\s*\{[^{}]*\}", "", s)
    s = re.sub(r"\\[,;:!]|\\q?quad(?![a-zA-Z])", " ", s)
    s = s.replace("~", " ").replace("\\\\", " ")
    return _WS.sub(" ", s).strip()


def extract_snippet(tex: str) -> str:
    body = tex.split("\\begin{document}", 1)
    if len(body) < 2:
        return ""
    body = body[1].split("\\end{document}", 1)[0]
    for chunk in re.split(r"\n\s*\n", body):
        para = detex(chunk)
        if len(para) >= 30:
            return para
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    # planetmath moves slowly (weekly cadence in the nightly plan)
    ap.add_argument("--max-age-hours", type=float, default=168.0)
    args = ap.parse_args()

    repos = list_content_repos()
    heads: dict[str, str] = {}
    for name in repos:
        heads[name] = sync_repo(name, args.max_age_hours)

    qmap = common.qid_map("planetmath")
    qmap_cf: dict[str, str] = {}
    for k in sorted(qmap):
        qmap_cf.setdefault(k.casefold(), qmap[k])

    pages_by_id: dict[str, dict] = {}
    related: dict[str, set[str]] = {}
    n_tex = n_skipped = 0
    for name in repos:
        repo_dir = common.CACHE_DIR / "planetmath" / name
        for tex_file in sorted(repo_dir.rglob("*.tex")):
            n_tex += 1
            tex = tex_file.read_text(encoding="utf-8", errors="replace")
            canonical = cmd_args(tex, "pmcanonicalname")
            titles = cmd_args(tex, "pmtitle")
            if not canonical or not titles or not CANONICAL_RE.match(canonical[0]):
                n_skipped += 1
                continue
            cid = canonical[0]
            rel = set()
            for arg in cmd_args(tex, "pmrelated"):
                rel.update(t for t in re.split(r"[,\s]+", arg.strip())
                           if CANONICAL_RE.match(t))
            if cid in pages_by_id:  # cross-listed in several MSC repos: merge
                related.setdefault(cid, set()).update(rel)
                row = pages_by_id[cid]
                syns = set(row.get("aliases", []))
                syns.update(detex(s) for s in cmd_args(tex, "pmsynonym"))
                syns = sorted(s for s in syns if s)
                if syns:
                    row["aliases"] = syns
                continue
            row = {"db": "planetmath", "id": cid, "title": detex(titles[0]),
                   "url": f"https://planetmath.org/{cid}"}
            snippet = extract_snippet(tex)
            if snippet:
                row["snippet"] = snippet
            syns = sorted({detex(s) for s in cmd_args(tex, "pmsynonym")} - {""})
            if syns:
                row["aliases"] = syns
            ptype = cmd_args(tex, "pmtype")
            if ptype and ptype[0].strip():
                row["kind_hint"] = ptype[0].strip().lower()
            qid = qmap.get(cid) or qmap_cf.get(cid.casefold())
            if qid:
                row["qid"] = qid
            pages_by_id[cid] = row
            related.setdefault(cid, set()).update(rel)

    pages = [pages_by_id[cid] for cid in sorted(pages_by_id)]
    links = [{"db": "planetmath", "src": src, "dst": dst, "context": "related"}
             for src in sorted(related) for dst in sorted(related[src])]
    pin = hashlib.sha1(
        json.dumps(sorted(heads.items())).encode()).hexdigest()[:12]
    common.emit("planetmath", pages, links, {
        "source_pin": f"{len(repos)} repos @ {pin}",
        "n_tex": n_tex, "n_skipped": n_skipped,
        "n_qid_joined": sum(1 for p in pages if "qid" in p),
    })


if __name__ == "__main__":
    main()
