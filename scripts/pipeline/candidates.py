#!/usr/bin/env python3
"""Stage 9c: candidate retrieval — the declarations a locate task is handed.

docs/PIPELINE.md section 3 (stage 9c). Ported from formal-companion's
``scripts/locate/candidates.py``, with the one substitution the ruling asks for: the
retrieval runs over **WikiLean's declaration index** (:mod:`index`), not over a
companion workspace's database. Two kinds of hit, kept apart because only one of them
binds gate rule 4:

* **curated** — a deterministic join that says "this declaration is about this
  article": the ``@[wikidata]`` tag a library carries on the article's own Wikidata
  item. That tag is WikiLean's own — ``bot/`` proposes it and a human reviews it — so
  the join is a lookup in the index and nothing else, and a curated hit the index does
  not hold is **dropped and named**, never kept as a phantom the gate would later
  refuse a worker for not locating. A worker must accept or reject every curated hit
  with a reason (gate rule 4).
* **lexical** — a deterministic token-overlap search over the index's declaration names
  and modules, for the article's own terms: its title and the lead's bold terms
  (:func:`source_terms`). A hint, never an obligation. A bold term is queried only when
  it is one (:func:`is_lexical_term`): at least :data:`MIN_TERM_LETTERS` letters, not
  only a paragraph label, and not only stop words — a bold "R" matches half a library
  and finds nothing.

**The score is this module's own, and it is deterministic.** formal-companion ranks by
an FTS engine's score; WikiLean has no such index, so a hit's score is the Sørensen–Dice
overlap of the query's tokens with the declaration's — its name split on ``.`` and
``_`` and at each camel-case boundary, plus its module's parts — rounded to four
decimals, and the order is: an exact identifier match first, then the score, then the
library and the declaration. A re-run over an unchanged index gives the same file.

The schema string stays ``cf-locate-candidates/1``, as the record set's and the
sourceset's do: the format is the contract, and the gate pins a candidates file by its
sha256.

    python3 scripts/pipeline/candidates.py --sourceset <dir> --index <file> \\
        --universe <file> --out <dir> [--source <id> …]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sourceset as sourceset_mod  # noqa: E402
import vocab  # noqa: E402

SCHEMA = "cf-locate-candidates/1"
#: how many lexical hits a source's file keeps
DEFAULT_LIMIT = 20
#: a term with fewer letters than this matches half a library
MIN_TERM_LETTERS = 3
#: a term made only of these is no term
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "is",
    "it", "its", "of", "on", "or", "that", "the", "then", "this", "to", "was", "with"})
#: the curated join this pipeline makes, and the only one
VIA_WIKIDATA_TAG = "wikidata_tag"

_WORD = re.compile(r"[0-9]+|[A-Za-z][a-z0-9']*|[A-Z]+(?![a-z])")
_TITLE_FOLD = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "′": "'"})


class CandidateProblem(ValueError):
    """An input the retrieval cannot run over."""


# ---- the retrieval view of the declaration index ---------------------------------

#: what a retrieval row says, beyond the key. ``wikidata`` is the ``@[wikidata]`` tag
#: (a QID or a list of them); the rest place the declaration for a reader. The tally's
#: view of the same index is :mod:`index`, which reads other keys of the same rows: one
#: file, two views, so an index built once answers both.
RETRIEVAL_KEYS = ("module", "kind", "wikidata")


def _qids(value) -> list[str]:
    """The QIDs a row's ``wikidata`` holds: one, a list, or none."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({q for q in (str(v) for v in value) if vocab.QID_RE.match(q)})


class Retrieval:
    """Every declaration of an index, as the retrieval reads them."""

    def __init__(self, rows: Iterable[dict], *, universe_sha256: str | None = None):
        self.universe_sha256 = universe_sha256
        self.rows: list[dict] = []
        self.by_qid: dict[str, list[dict]] = {}
        self.by_key: dict[tuple[str, str], dict] = {}
        for raw in rows:
            library = str(raw.get("library") or "")
            name = str(raw.get("declaration") or raw.get("name") or "")
            if not library or not name:
                continue
            row = {"library": library, "declaration": name,
                   "commit": raw.get("commit") or raw.get("commit_sha"),
                   **{key: raw.get(key) for key in RETRIEVAL_KEYS}}
            row["tokens"] = declaration_tokens(name, row.get("module"))
            self.rows.append(row)
            self.by_key[(library, name)] = row
            for qid in _qids(row.get("wikidata")):
                self.by_qid.setdefault(qid, []).append(row)

    @classmethod
    def load(cls, path: Path) -> "Retrieval":
        """Read a ``.jsonl`` snapshot or a SQLite ``decls`` table, by its suffix."""
        where = Path(path)
        if where.suffix in (".jsonl", ".json"):
            return cls(_jsonl_rows(where), universe_sha256=_jsonl_universe(where))
        return cls(*_sqlite_rows(where))

    def holds(self, library: str, declaration: str) -> bool:
        return (library, declaration) in self.by_key

    def __len__(self) -> int:
        return len(self.rows)


def _jsonl_universe(path: Path) -> str | None:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if set(value) <= {"universe", "universe_sha256", "schema"}:
                    return value.get("universe_sha256")
                return None
    return None


def _jsonl_rows(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CandidateProblem(f"{path} line {number}: not an object")
            if number == 1 and set(value) <= {"universe", "universe_sha256", "schema"}:
                continue
            yield value


def _sqlite_rows(path: Path) -> tuple[list[dict], None] | tuple[list[dict], str]:
    if not Path(path).is_file():
        raise CandidateProblem(f"no declaration index at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        have = {row[1] for row in conn.execute("PRAGMA table_info(decls)")}
        if not have:
            raise CandidateProblem(f"{path} has no decls table")
        rows = [dict(row) for row in conn.execute("SELECT * FROM decls")]
        for row in rows:
            row["declaration"] = row.get("name")
            if isinstance(row.get("wikidata"), str) and row["wikidata"].startswith("["):
                row["wikidata"] = json.loads(row["wikidata"])
        try:
            found = conn.execute(
                "SELECT value FROM meta WHERE key = 'universe_sha256'").fetchone()
        except sqlite3.Error:
            found = None
        return rows, (str(found[0]) if found else None)
    finally:
        conn.close()


# ---- the terms ---------------------------------------------------------------------

def declaration_tokens(name: str, module=None) -> frozenset[str]:
    """A declaration's own words: its name split on ``.``, ``_`` and camel case.

    Its module's parts join them, so ``Mathlib.Topology.Basic``'s declarations answer to
    "topology" even when their own names do not say it.
    """
    text = str(name).replace(".", " ").replace("_", " ")
    if module:
        text += " " + str(module).replace(".", " ").replace("_", " ")
    return frozenset(word.lower() for word in _WORD.findall(text))


def query_tokens(term: str) -> frozenset[str]:
    """A query term's words, folded and stripped of the stop words."""
    folded = unicodedata.normalize("NFKC", str(term or "")).translate(_TITLE_FOLD)
    return frozenset(word.lower() for word in _WORD.findall(folded)) - STOP_WORDS


def is_lexical_term(term) -> bool:
    """Is this worth querying: enough letters, not a label, and not only stop words?"""
    text = str(term or "").strip()
    if sum(1 for ch in text if ch.isalpha()) < MIN_TERM_LETTERS:
        return False
    if vocab.is_bold_label(text):
        return False
    return bool(query_tokens(text))


def source_terms(entry: dict, sources_dir: Path) -> list[tuple[str, str]]:
    """``[(term, where)]``: the article's title, then the lead's bold terms."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    title = str(entry.get("title") or "")
    if is_lexical_term(title):
        out.append((title, "title"))
        seen.add(title.casefold())
    path = Path(sources_dir) / entry["source_id"] / "finders.json"
    rows = []
    if path.is_file():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CandidateProblem(f"{path}: not JSON ({exc})") from None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("finder") != vocab.FINDER_BOLD_DEFINITION:
            continue
        if row.get("section"):          # the lead's bold terms, and no others
            continue
        term = str(row.get("term") or "")
        if is_lexical_term(term) and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append((term, "term"))
    return out


# ---- the two kinds of hit ------------------------------------------------------------

def curated_hits(qid: str | None, retrieval: Retrieval) -> list[dict]:
    """Every declaration the index says carries the article's ``@[wikidata]`` tag."""
    if not qid:
        return []
    hits = [{"library": row["library"], "declaration": row["declaration"],
             "via": VIA_WIKIDATA_TAG, "evidence": f"@[wikidata {qid}]"}
            for row in retrieval.by_qid.get(str(qid), [])]
    return sorted(hits, key=lambda hit: (hit["library"], hit["declaration"]))


def score(query: frozenset[str], tokens: frozenset[str]) -> float:
    """Sørensen–Dice overlap of two token sets, to four decimals."""
    if not query or not tokens:
        return 0.0
    return round(2 * len(query & tokens) / (len(query) + len(tokens)), 4)


def lexical_hits(terms: Iterable[tuple[str, str]], retrieval: Retrieval, *,
                 limit: int = DEFAULT_LIMIT) -> list[dict]:
    """The index's best matches for the article's terms, best first.

    A declaration whose own last name component is the query, joined, comes first
    (``identifier``); the rest follow by score. One row per declaration: the best term
    wins, and the term it won by is recorded as ``matched``.
    """
    best: dict[tuple[str, str], dict] = {}
    for term, where in terms:
        query = query_tokens(term)
        if not query:
            continue
        joined = "".join(sorted(query))
        for row in retrieval.rows:
            value = score(query, row["tokens"])
            if value <= 0:
                continue
            last = str(row["declaration"]).rsplit(".", 1)[-1]
            identifier = "".join(sorted(query_tokens(last))) == joined
            key = (row["library"], row["declaration"])
            hit = {"library": row["library"], "declaration": row["declaration"],
                   "matched": where, "score": value, "identifier": identifier}
            found = best.get(key)
            if found is None or (hit["identifier"], hit["score"]) > \
                    (found["identifier"], found["score"]):
                best[key] = hit
    ordered = sorted(best.values(),
                     key=lambda hit: (not hit["identifier"], -hit["score"],
                                      hit["library"], hit["declaration"]))
    return [{"library": hit["library"], "declaration": hit["declaration"],
             "matched": hit["matched"], "score": hit["score"]}
            for hit in ordered[:max(0, limit)]]


def build(entry: dict, retrieval: Retrieval, sources_dir: Path, *, universe: str,
          universe_sha256: str, index_sha256: str, limit: int = DEFAULT_LIMIT) -> dict:
    """One source's candidates file."""
    terms = source_terms(entry, sources_dir)
    qid = entry.get("wikidata")
    curated = curated_hits(qid, retrieval)
    return {
        "schema": SCHEMA,
        "source_id": entry["source_id"],
        "source_sha": entry.get("html_sha256"),
        "article_title": entry.get("title"),
        "article_qid": qid,
        "universe": universe,
        "universe_sha256": universe_sha256,
        "index": {"sha256": index_sha256, "declarations": len(retrieval)},
        "terms": [term for term, _where in terms],
        "curated": curated,
        "dropped": [],
        "lexical": lexical_hits(terms, retrieval, limit=limit),
    }


def write(directory: Path, document: dict) -> str:
    """Write one candidates file and return its sha256 — what ``gate.json`` pins."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    (directory / f"{document['source_id']}.json").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(*, sourceset_dir: Path, index: Path, universe: Path, out: Path,
        source_ids: Iterable[str] = (), sourceset_id: str | None = None,
        limit: int = DEFAULT_LIMIT) -> dict:
    """Write one candidates file per planned source; returns ``{source_id: sha256}``."""
    ss = sourceset_mod.Sourceset.open(sourceset_dir, sourceset_id)
    manifest = ss.latest()
    universe_doc = json.loads(Path(universe).read_text(encoding="utf-8"))
    retrieval = Retrieval.load(Path(index))
    want = str(universe_doc.get("universe_sha256") or "")
    if retrieval.universe_sha256 and want and retrieval.universe_sha256 != want:
        raise CandidateProblem(
            f"the declaration index was built for universe "
            f"{retrieval.universe_sha256[:12]} and the retrieval is for {want[:12]}")
    index_sha = hashlib.sha256(Path(index).read_bytes()).hexdigest()
    wanted = list(source_ids) or [sid for sid in manifest.preferred().values()
                                  if sourceset_mod.skip_reason(manifest.get(sid)) is None]
    shas: dict[str, str] = {}
    for sid in sorted(wanted):
        entry = manifest.get(sid)
        if entry is None:
            raise CandidateProblem(f"the sourceset holds no entry for {sid}")
        document = build(entry, retrieval, ss.dir / "sources",
                         universe=str(universe_doc.get("id") or ""),
                         universe_sha256=want, index_sha256=index_sha, limit=limit)
        shas[sid] = write(Path(out), document)
    return shas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sourceset", required=True, type=Path)
    parser.add_argument("--sourceset-id")
    parser.add_argument("--index", required=True, type=Path,
                        help="the declaration index: a .jsonl snapshot or a SQLite file")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path,
                        help="the candidates directory to write")
    parser.add_argument("--source", action="append", default=[], dest="source_ids")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    try:
        shas = run(sourceset_dir=args.sourceset, index=args.index,
                   universe=args.universe, out=args.out, source_ids=args.source_ids,
                   sourceset_id=args.sourceset_id, limit=args.limit)
    except (CandidateProblem, sourceset_mod.SourcesetError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{len(shas)} candidates file(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
