#!/usr/bin/env python3
"""The declaration index: what the tally joins a location's proof status from.

docs/PIPELINE.md section 3 (stage 8). A locate document says *which* declaration a
passage is at; it never says what that declaration *is*. The proof status, the
Prop-valued-definition flag, the module and the source position are joined here, from
an index of the universe's libraries built mechanically at index time — never read from
a document, and never decided by a model. A document that writes ``proof_status`` is
named as a problem and the written value ignored.

The index is an **input** to the tally with a stated shape, not a service this module
provides: where WikiLean's own copy lives — a SQLite of its own under this directory,
or the Brain, which is already a map of Lean libraries — is open (docs/PIPELINE.md
section 11, question 3). Two readers of that shape are here:

* :class:`JsonlIndex` — a portable snapshot, one JSON object per line. This is what a
  contributor carries and what a test uses.
* :class:`SqliteIndex` — a ``decls`` table, the columns below. The database is accepted
  for a universe by its recorded ``universe_sha256`` and not by its bytes: a rebuild of
  an unchanged universe is the same index whatever file it is in.

Ported from ``corpus/query.declaration`` and the ``decls`` schema of
``corpus/build_db.py``; nothing of the Lean probe that fills it moves here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

#: What one row of the index says about a declaration. ``proof_status`` and
#: ``statement`` are the two the status rules read; the rest place it for a reader.
ROW_KEYS = ("proof_status", "statement", "module", "path", "line", "end_line", "kind")
#: The columns a ``decls`` table must have for :class:`SqliteIndex` to read it.
DECLS_COLUMNS = ("library", "commit_sha", "name", *ROW_KEYS)


class IndexProblem(ValueError):
    """An index that cannot be read, or one built for another universe."""


def _row(raw: dict) -> dict:
    """One index row as the tally reads it: the documented keys, ``statement`` a bool."""
    out = {key: raw.get(key) for key in ROW_KEYS}
    out["statement"] = bool(out["statement"])
    return out


class DeclarationIndex:
    """The one thing the tally asks of an index: what is this declaration?"""

    #: The universe the index was built for, when it records one.
    universe_sha256: str | None = None

    def declaration(self, library: str, name: str, commit: str) -> dict | None:
        """``{proof_status, statement, module, path, line, end_line, kind}``, or None.

        The commit is part of the key: an index may hold several commits of a library,
        and a location is judged against the commit its universe pinned.
        """
        raise NotImplementedError

    def check_universe(self, universe_sha256: str) -> None:
        """Refuse an index built for another universe, by its recorded sha."""
        if self.universe_sha256 and self.universe_sha256 != universe_sha256:
            raise IndexProblem(
                f"the declaration index was built for universe {self.universe_sha256[:12]} and "
                f"the run was planned against {universe_sha256[:12]}: a run is judged against "
                "the universe frozen into its plan")

    def close(self) -> None:
        """Release whatever the index holds; safe to call twice."""


class JsonlIndex(DeclarationIndex):
    """A snapshot of the index: one ``{library, commit, declaration, ...}`` per line.

    ``commit`` may be omitted on a snapshot of a single-commit universe, in which case
    the row answers for whatever commit is asked — a snapshot of one universe is one
    commit per library by construction. A declaration listed twice for one commit is
    refused: a snapshot states one answer per key or it is not one.
    """

    def __init__(self, rows: Iterable[dict], *, universe_sha256: str | None = None):
        self.universe_sha256 = universe_sha256
        self._exact: dict[tuple[str, str, str], dict] = {}
        self._any_commit: dict[tuple[str, str], dict] = {}
        for number, raw in enumerate(rows, 1):
            if not isinstance(raw, dict):
                raise IndexProblem(f"index row {number} is not an object")
            library = str(raw.get("library") or "")
            name = str(raw.get("declaration") or raw.get("name") or "")
            if not library or not name:
                raise IndexProblem(f"index row {number} names no library and declaration")
            row = _row(raw)
            commit = raw.get("commit") or raw.get("commit_sha")
            if commit:
                key = (library, str(commit), name)
                if key in self._exact and self._exact[key] != row:
                    raise IndexProblem(f"index row {number}: {library}:{name} at "
                                       f"{str(commit)[:12]} is listed twice, differently")
                self._exact[key] = row
            else:
                key2 = (library, name)
                if key2 in self._any_commit and self._any_commit[key2] != row:
                    raise IndexProblem(f"index row {number}: {library}:{name} is listed twice, "
                                       "differently, with no commit to tell the two apart")
                self._any_commit[key2] = row

    @classmethod
    def load(cls, path: Path) -> "JsonlIndex":
        """Read a ``.jsonl`` snapshot; a leading ``{"universe_sha256": ...}`` line pins it."""
        rows: list[dict] = []
        universe: str | None = None
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError as exc:
                    raise IndexProblem(f"{path} line {number}: not JSON ({exc})") from None
                if not isinstance(value, dict):
                    raise IndexProblem(f"{path} line {number}: not an object")
                if number == 1 and set(value) <= {"universe", "universe_sha256", "schema"}:
                    universe = value.get("universe_sha256")
                    continue
                rows.append(value)
        return cls(rows, universe_sha256=universe)

    def declaration(self, library: str, name: str, commit: str) -> dict | None:
        got = self._exact.get((library, commit, name))
        if got is not None:
            return dict(got)
        got = self._any_commit.get((library, name))
        return dict(got) if got is not None else None

    def __len__(self) -> int:
        return len(self._exact) + len(self._any_commit)


class SqliteIndex(DeclarationIndex):
    """A ``decls`` table, opened read-only."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise IndexProblem(f"no declaration index at {self.path}")
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(decls)")}
        if not have:
            raise IndexProblem(f"{self.path} has no decls table")
        missing = [name for name in DECLS_COLUMNS if name not in have]
        if missing:
            raise IndexProblem(f"{self.path}: the decls table has no "
                               f"{', '.join(missing)} column(s)")
        self.universe_sha256 = self._meta("universe_sha256")

    def _meta(self, key: str) -> str | None:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return str(row[0]) if row is not None else None

    def declaration(self, library: str, name: str, commit: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM decls WHERE library = ? AND name = ? AND commit_sha = ?",
            (library, name, commit)).fetchone()
        return _row(dict(row)) if row is not None else None

    def close(self) -> None:
        self._conn.close()


class EmptyIndex(DeclarationIndex):
    """No index at all: every location joins to nothing.

    Not a default — the tally refuses to derive a status without an index, because a
    passage whose locations all failed to join looks exactly like an unresolved one.
    It exists so a caller that only wants the finding rows can say so.
    """

    def declaration(self, library: str, name: str, commit: str) -> dict | None:
        return None


def open_index(path: Path | str | None, *, universe_sha256: str | None = None
               ) -> DeclarationIndex:
    """Open a ``.jsonl`` snapshot or a SQLite index by its suffix, and pin its universe."""
    if path is None:
        return EmptyIndex()
    where = Path(path)
    index: DeclarationIndex
    if where.suffix in (".jsonl", ".json"):
        index = JsonlIndex.load(where)
    else:
        index = SqliteIndex(where)
    if universe_sha256:
        index.check_universe(universe_sha256)
    return index


def snapshot_line(library: str, commit: str, declaration: str, row: dict) -> dict:
    """One row of a ``.jsonl`` snapshot, as :class:`JsonlIndex` reads it back."""
    out: dict[str, Any] = {"library": library, "commit": commit, "declaration": declaration}
    out.update({key: row.get(key) for key in ROW_KEYS})
    out["statement"] = bool(out["statement"])
    return out
