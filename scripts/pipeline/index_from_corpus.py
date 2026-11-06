#!/usr/bin/env python3
"""Stage 8: WikiLean's declaration index, converted from a universe's corpus database.

docs/PIPELINE.md section 3 (stage 8), and section 11's open question 3 — *where does
WikiLean's declaration index come from?* One answer, for a universe that was already
harvested once: a ``cf-corpus/1`` SQLite, which is a Lean probe's own output for the
libraries of a universe **at the commits that universe pins**. This module reads that
file's data and writes :mod:`index`'s own format. It is a converter and nothing else:
no formal-companion code is imported, nothing of the probe moves here, and the only
thing taken from the database is what its tables say.

What it reads, and what each column becomes:

===========================  =====================================================
``decls.library``            the index row's ``library``
``decls.commit_sha``         its ``commit`` — the key is the triple, never the name
``decls.name``               its ``declaration``
the seven :data:`index.ROW_KEYS`  ``proof_status``, ``statement``, ``module``,
                             ``path``, ``line``, ``end_line``, ``kind``, each read
                             straight across: a ``cf-corpus/1`` ``decls`` table
                             states them in the same vocabulary
                             (:data:`vocab.PROOF_STATUSES`), so nothing is inferred
                             from a signature or a body here
``tags`` where ``tag`` is    the row's ``wikidata``: the literal ``@[wikidata]``
``'wikidata'``               attribute, which is the join
                             :mod:`candidates` makes its **curated** hits from and
                             gate rule 4 holds a worker to
===========================  =====================================================

**The tags.** A corpus that carries its own ``@[wikidata]`` rows is the authority for
them: they are the attribute as it stands in the library at the pinned commit.
``--catalog`` joins ``catalog/data/*.jsonl`` as well — the tagging bot's reviewed
declaration↔QID rows — for a corpus that carries none; it is off by default, because a
tag the libraries do not carry is a curated hit a worker then owes an answer on
(gate rule 4) for a join the published libraries never made.

**The search corpus** (``--search``) is the same data again in a shape a sandboxed
worker can read with Read, Grep and Glob and no shell: four tab-separated files, one
line per declaration, plus the ``README.md`` that says how to search them. The index is
what the *tally* and the *gate* join on; the search corpus is what a *worker* looks a
declaration up in. They are written together so they cannot drift.

    python3 scripts/pipeline/index_from_corpus.py --corpus <file.sqlite> \\
        --out <file.jsonl> [--universe <file.yaml|json>] [--search <dir>] \\
        [--catalog <dir>] [--provenance <file.json>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import index as index_mod  # noqa: E402
import vocab  # noqa: E402

#: What this writes on the snapshot's first line, and what :class:`index.JsonlIndex`
#: reads back off it.
INDEX_SCHEMA = "wikilean.pipeline.index/1"
#: The corpus schemas this converter knows. A file saying anything else is refused by
#: name rather than read on the assumption its columns mean the same thing.
CORPUS_SCHEMAS = ("cf-corpus/1",)
#: The ``decls`` columns a conversion needs. ``statement`` is the Prop-valued-definition
#: flag; the rest are the key and the six remaining :data:`index.ROW_KEYS`.
DECLS_COLUMNS = ("library", "commit_sha", "name", "proof_status", "statement",
                 "module", "path", "line", "end_line", "kind")
#: The ``decls`` columns the search corpus adds, and which are **not** index keys.
SEARCH_COLUMNS = ("signature", "docstring")
#: The tag a curated hit is joined on.
WIKIDATA_TAG = "wikidata"
#: The files ``--search`` writes, each a tab-separated table a worker greps.
SEARCH_FILES = ("declarations.tsv", "signatures.tsv", "docstrings.tsv",
                "wikidata.tsv", "README.md")

_CONTROL = re.compile(r"[\t\r\n\v\f\x00-\x08\x0e-\x1f\x7f]+")


class CorpusIndexProblem(ValueError):
    """A corpus this converter will not read, named rather than converted anyway."""


# ---- the database ------------------------------------------------------------------


def open_corpus(path: Path) -> sqlite3.Connection:
    """Open a corpus read-only and refuse one this converter does not know."""
    where = Path(path)
    if not where.is_file():
        raise CorpusIndexProblem(f"no corpus database at {where}")
    conn = sqlite3.connect(f"file:{where}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        schema = meta(conn, "schema")
        if schema not in CORPUS_SCHEMAS:
            raise CorpusIndexProblem(
                f"{where} says it is a {schema!r} corpus and this converter reads "
                f"{', '.join(CORPUS_SCHEMAS)}; its columns are not assumed to mean the same")
        have = {row["name"] for row in conn.execute("PRAGMA table_info(decls)")}
        if not have:
            raise CorpusIndexProblem(f"{where} has no decls table")
        missing = [name for name in DECLS_COLUMNS if name not in have]
        if missing:
            raise CorpusIndexProblem(f"{where}: the decls table has no "
                                     f"{', '.join(missing)} column(s)")
    except Exception:
        conn.close()
        raise
    return conn


def meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None else None


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                       (name,)).fetchone()
    return row is not None


def libraries(conn: sqlite3.Connection) -> dict[str, str]:
    """``{library id: commit}`` the corpus was built for, when it lists them."""
    if not has_table(conn, "libraries"):
        return {}
    return {str(row["id"]): str(row["commit_sha"])
            for row in conn.execute("SELECT id, commit_sha FROM libraries")}


def wikidata_tags(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """``{decl id: [QID]}`` from the corpus's own ``@[wikidata]`` attribute rows."""
    if not has_table(conn, "tags"):
        return {}
    out: dict[int, set[str]] = {}
    for row in conn.execute("SELECT decl_id, value FROM tags WHERE tag = ?",
                            (WIKIDATA_TAG,)):
        qid = str(row["value"] or "").strip()
        if vocab.QID_RE.match(qid):
            out.setdefault(int(row["decl_id"]), set()).add(qid)
    return {decl_id: sorted(qids) for decl_id, qids in out.items()}


# ---- the universe the index is pinned to ---------------------------------------------


def universe_pin(path: Path | None, conn: sqlite3.Connection) -> tuple[str, str]:
    """``(universe id, universe_sha256)``, and refuse a corpus built for another one.

    The corpus states its own; ``--universe`` states the run's. Where both are given
    they must agree, because an index is accepted for a universe by its recorded sha
    (:meth:`index.DeclarationIndex.check_universe`) and a mismatch here would be a
    snapshot stamped with a universe it was not harvested at.
    """
    corpus_id, corpus_sha = meta(conn, "universe"), meta(conn, "universe_sha256")
    if path is None:
        if not corpus_sha:
            raise CorpusIndexProblem(
                "the corpus records no universe_sha256 and no --universe was given: an "
                "index that names no universe cannot be refused for the wrong one")
        return str(corpus_id or ""), corpus_sha
    wanted_id, wanted_sha = read_universe(Path(path))
    if corpus_sha and wanted_sha and corpus_sha != wanted_sha:
        raise CorpusIndexProblem(
            f"the corpus was built for universe {corpus_sha[:12]} and --universe names "
            f"{wanted_sha[:12]}: a declaration is judged at the commit its universe pinned")
    return wanted_id or str(corpus_id or ""), wanted_sha or str(corpus_sha or "")


def read_universe(path: Path) -> tuple[str, str]:
    """``(id, universe_sha256)`` of a universe file, JSON or the ``universe.yaml``.

    Only the two scalars at the top level are read, with a line reader rather than a
    YAML parser: this module adds no dependency for two strings, and a universe file
    whose ``id`` or ``universe_sha256`` is not a plain top-level scalar is refused.
    """
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".json":
        value = json.loads(text)
        if not isinstance(value, dict):
            raise CorpusIndexProblem(f"{path} is not a universe object")
        return str(value.get("id") or ""), str(value.get("universe_sha256") or "")
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^(id|universe_sha256):\s*\"?([A-Za-z0-9_.-]+)\"?\s*$", line)
        if match:
            found.setdefault(match.group(1), match.group(2))
    if "universe_sha256" not in found:
        raise CorpusIndexProblem(f"{path} states no top-level universe_sha256")
    return found.get("id", ""), found["universe_sha256"]


# ---- the catalog join, for a corpus that carries no tags -------------------------------

#: The tagging bot's files that pair a Wikidata QID with a declaration it proposed and a
#: human reviewed. Read only under ``--catalog``.
CATALOG_TAGGED = ("mathlib_yaml_tagged.jsonl", "pilot_tagged.jsonl", "tier2_tagged.jsonl",
                  "refresh_tagged.jsonl", "generated_candidates.jsonl")


def _jsonl(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "_meta" not in value:
                yield value


def catalog_tags(directory: Path) -> dict[str, list[str]]:
    """``{declaration: [QID]}`` from the tagging bot's reviewed rows.

    Keyed by declaration name alone, because the bot's rows name Mathlib declarations
    and not a library and a commit; the caller joins them onto whatever library of the
    corpus holds that name.
    """
    out: dict[str, set[str]] = {}

    def add(name, qid) -> None:
        name, qid = str(name or ""), str(qid or "")
        if name and vocab.QID_RE.match(qid):
            out.setdefault(name, set()).add(qid)

    directory = Path(directory)
    for filename in CATALOG_TAGGED:
        for row in _jsonl(directory / filename):
            qid = row.get("wikidata_qid") or row.get("primary_qid")
            add(row.get("primary_decl"), qid)
            for decl in row.get("mathlib_decls") or []:
                if isinstance(decl, dict):
                    add(decl.get("decl"), qid)
    for row in _jsonl(directory / "concept_layer.jsonl"):
        add(row.get("primary_decl"), row.get("qid"))
        for decl in row.get("secondary_decls") or []:
            if isinstance(decl, dict):
                add(decl.get("decl"), row.get("qid"))
    for row in _jsonl(directory / "fc_tagged.jsonl"):
        add(row.get("fc_decl"), row.get("qid"))
    for row in _jsonl(directory / "mathlib_tag_xrefs.jsonl"):
        if row.get("db") == WIKIDATA_TAG:
            add(row.get("decl"), row.get("tag"))
    return {name: sorted(qids) for name, qids in out.items()}


# ---- the conversion --------------------------------------------------------------------


def index_row(raw, qids: Iterable[str] = ()) -> dict:
    """One ``.jsonl`` line: the key, the seven row keys, and the tag when there is one."""
    out: dict = {"library": str(raw["library"]), "commit": str(raw["commit_sha"]),
                 "declaration": str(raw["name"])}
    for key in index_mod.ROW_KEYS:
        out[key] = raw[key]
    out["statement"] = bool(out["statement"])
    qids = sorted(qids)
    if qids:
        out[WIKIDATA_TAG] = qids
    return out


def convert(*, corpus: Path, out: Path, universe: Path | None = None,
            search: Path | None = None, catalog: Path | None = None) -> dict:
    """Write the index (and the search corpus) and return what went into them."""
    conn = open_corpus(Path(corpus))
    try:
        universe_id, universe_sha = universe_pin(universe, conn)
        pinned = libraries(conn)
        tags = wikidata_tags(conn)
        joined = catalog_tags(Path(catalog)) if catalog is not None else {}
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        writer = _SearchWriter(Path(search)) if search is not None else None
        rows = tagged = joined_rows = skewed = 0
        by_library: Counter = Counter()
        by_status: Counter = Counter()
        tag_source: Counter = Counter()
        columns = ", ".join(DECLS_COLUMNS)
        extra = [name for name in SEARCH_COLUMNS
                 if name in {row["name"] for row in conn.execute(
                     "PRAGMA table_info(decls)")}]
        select = (f"SELECT id, {columns}"
                  + ("".join(f", {name}" for name in extra) if extra else "")
                  + " FROM decls ORDER BY library, name, commit_sha")
        try:
            with out.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"schema": INDEX_SCHEMA, "universe": universe_id,
                                         "universe_sha256": universe_sha}) + "\n")
                for raw in conn.execute(select):
                    library, name = str(raw["library"]), str(raw["name"])
                    commit = str(raw["commit_sha"])
                    if pinned and pinned.get(library) not in (None, commit):
                        # a corpus holding two commits of one library: the universe pins
                        # one, and an index row at the other would answer for a commit
                        # nobody planned against
                        skewed += 1
                        continue
                    qids = list(tags.get(int(raw["id"]), ()))
                    if qids:
                        tag_source["corpus @[wikidata]"] += len(qids)
                    elif joined.get(name):
                        qids = list(joined[name])
                        joined_rows += 1
                        tag_source["catalog decl/QID rows"] += len(qids)
                    row = index_row(raw, qids)
                    handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                    rows += 1
                    tagged += 1 if qids else 0
                    by_library[library] += 1
                    by_status[str(row["proof_status"])] += 1
                    if writer is not None:
                        writer.add(raw, qids, extra)
        finally:
            if writer is not None:
                writer.close()
        unknown = sorted(set(by_status) - vocab.PROOF_STATUSES)
        if unknown:
            raise CorpusIndexProblem(
                f"the corpus states proof_status {', '.join(unknown)}, which "
                f"scripts/pipeline/status.py does not read; the index would carry a "
                f"status no rule knows")
        provenance = {
            "schema": INDEX_SCHEMA,
            "corpus": {"path": Path(corpus).name,
                       "sha256": sha256_file(Path(corpus)),
                       "schema": meta(conn, "schema"), "built": meta(conn, "built")},
            "universe": universe_id, "universe_sha256": universe_sha,
            "index": {"path": str(out), "sha256": sha256_file(out), "rows": rows},
            "rows": rows,
            "rows_with_wikidata_tag": tagged,
            "rows_tagged_from_catalog": joined_rows,
            "tags_by_source": dict(tag_source.most_common()),
            "libraries": len(by_library),
            "libraries_pinned_by_the_corpus": len(pinned),
            "declarations_by_library": dict(by_library.most_common()),
            "declarations_by_proof_status": dict(by_status.most_common()),
            "rows_at_another_commit_than_the_universe_pins": skewed,
            "search": writer.provenance() if writer is not None else None,
        }
        return provenance
    finally:
        conn.close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def one_line(value) -> str:
    """A field as one tab-separated line: tabs, newlines and controls folded to spaces."""
    return _CONTROL.sub(" ", str(value if value is not None else "")).strip()


class _SearchWriter:
    """The search corpus: what a worker with Read, Grep and Glob looks a declaration up in.

    Four tables, one line per declaration, the first two columns always
    ``library <TAB> declaration`` so a hit in one file is looked up in the others. They
    are plain text on purpose: ripgrep over them is the search, and a worker that has no
    shell still has Grep.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.counts = Counter()
        self._handles = {
            name: (self.dir / name).open("w", encoding="utf-8")
            for name in ("declarations.tsv", "signatures.tsv", "docstrings.tsv",
                         "wikidata.tsv")}

    def add(self, raw, qids, extra: list[str]) -> None:
        library, name = str(raw["library"]), str(raw["name"])
        head = f"{library}\t{name}"
        self._handles["declarations.tsv"].write(
            f"{head}\t{one_line(raw['kind'])}\t{one_line(raw['proof_status'])}\t"
            f"{one_line(raw['module'])}\t{one_line(raw['path'])}:{one_line(raw['line'])}\n")
        self.counts["declarations"] += 1
        if "signature" in extra and one_line(raw["signature"]):
            self._handles["signatures.tsv"].write(f"{head}\t{one_line(raw['signature'])}\n")
            self.counts["signatures"] += 1
        if "docstring" in extra and one_line(raw["docstring"]):
            self._handles["docstrings.tsv"].write(f"{head}\t{one_line(raw['docstring'])}\n")
            self.counts["docstrings"] += 1
        for qid in qids:
            self._handles["wikidata.tsv"].write(f"{head}\t{qid}\n")
            self.counts["wikidata"] += 1

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        (self.dir / "README.md").write_text(SEARCH_README, encoding="utf-8")

    def provenance(self) -> dict:
        return {"dir": str(self.dir), "lines": dict(sorted(self.counts.items())),
                "files": {name: sha256_file(self.dir / name) for name in SEARCH_FILES}}


SEARCH_README = """\
# The declaration search corpus

Every declaration of this run's universe, at the commits the universe pins, in four
tab-separated tables. They are here so that a search is a **Grep**, and no shell is
needed. Each line begins with the same two columns, so a hit in one table is looked up
in the others by the pair it starts with.

| file | columns |
|---|---|
| `declarations.tsv` | `library` · `declaration` · `kind` · `proof_status` · `module` · `path:line` |
| `signatures.tsv` | `library` · `declaration` · the Lean signature, on one line |
| `docstrings.tsv` | `library` · `declaration` · the docstring, on one line |
| `wikidata.tsv` | `library` · `declaration` · a `@[wikidata]` QID it carries |

How to search:

* by name — `Grep` `declarations.tsv` for the identifier, or part of it. Lean names are
  dot-separated and camel-cased, so a pattern like `Monoid.*Hom` finds more than
  `MonoidHom` does.
* by what it says — `Grep` `signatures.tsv` for the operator, the type or the binder
  (`IsOpen`, `deriv`, `∀ .*Group`), and `docstrings.tsv` for the words a library would
  use to describe it.
* by module — the last column of `declarations.tsv`; a field's declarations live
  together, so one good hit's module names the place to look for its neighbours.
* `Read` any of them with an offset when a Grep hit needs its surroundings.

The tables are the universe and nothing else: a declaration that is not in them is not
in the universe, and naming one in a document is refused by the gate.
"""


# ---- the command line --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, type=Path,
                        help="the cf-corpus/1 SQLite to read")
    parser.add_argument("--out", required=True, type=Path,
                        help="the .jsonl declaration index to write")
    parser.add_argument("--universe", type=Path,
                        help="the universe the run is planned against (yaml or json); "
                             "a corpus built for another one is refused")
    parser.add_argument("--search", type=Path,
                        help="also write the worker's read-only search corpus here")
    parser.add_argument("--catalog", type=Path,
                        help="catalog/data, to join the tagging bot's declaration/QID "
                             "rows onto declarations the corpus carries no tag for")
    parser.add_argument("--provenance", type=Path,
                        help="write what went into the index here")
    args = parser.parse_args(argv)
    try:
        provenance = convert(corpus=args.corpus, out=args.out, universe=args.universe,
                             search=args.search, catalog=args.catalog)
    except (CorpusIndexProblem, OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.provenance:
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.write_text(json.dumps(provenance, indent=1) + "\n",
                                   encoding="utf-8")
    print(f"{provenance['rows']} declaration(s) over {provenance['libraries']} "
          f"librar(ies), {provenance['rows_with_wikidata_tag']} with a @[wikidata] tag "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
