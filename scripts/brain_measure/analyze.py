"""Step 0's measurements of one built scope: sizes, the offline import plan, query times and the
verify-style digest time. Called by measure.py's `analyze` stage; writes DIR/<scope>/analysis.json.

The import plan is the real importer (site/ops/brain_d1_import.py's Importer, its range and
statement planning unchanged) run against a local SQLite file standing in for D1, created from
wiki/migrations-brain/0001_brain.sql; the runner executes each `--command` the way the
importer's own tests do (test_brain_d1_import.split_sql). Nothing contacts D1 or runs wrangler.
The stand-in's file after the import is the projected D1 footprint of the dataset: D1 stores a
SQLite database, and the file holds what D1 would (the `ds` columns, every UNIQUE index, both FTS
tables with contentless_delete, the control tables). The name index of scope B is not copied by
the importer; its rows are planned with the same statement rule (plan_rows) and executed into the
same file, so its footprint is measured the same way.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path

PAGE_SIZE = 4096
D1_CAP = 10 * 10**9                 # 10 GB per database on Workers Paid
STATEMENT_LIMIT = 100_000           # D1's statement text limit (100 KB)


# ---- small pure helpers (tested) --------------------------------------------------------------

def percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile of `values` (0 for no values)."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[int(rank) - 1]


def sql_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def plan_rows(table: str, columns: list[str], rows, budget: int, *, ds: int | None = None):
    """Statements copying `rows` (seq first) into `table` by the importer's rule for a data
    table (Importer._plan_table): per range, a DELETE of its seq span and one multi-row INSERT
    whose text stays within `budget` bytes. Rows are integers and strings only. A row longer
    than the budget on its own raises (the importer would append it in chunks; the name index
    has no such row)."""
    cols = (["ds"] if ds is not None else []) + columns
    header = f"INSERT INTO {table}({', '.join(cols)}) VALUES "
    hlen = len(header.encode())
    tuples: list[str] = []
    size = hlen
    lo = hi = None

    def literal(v):
        return "NULL" if v is None else str(v) if isinstance(v, int) else sql_quote(v)

    def flush():
        where = (f"ds = {ds} AND " if ds is not None else "") + f"seq BETWEEN {lo} AND {hi}"
        return [f"DELETE FROM {table} WHERE {where}", header + ", ".join(tuples)]

    for row in rows:
        values = ([ds] if ds is not None else []) + list(row)
        lit = "(" + ", ".join(literal(v) for v in values) + ")"
        cost = len(lit.encode())
        if hlen + cost > budget:
            raise ValueError(f"{table} seq {row[0]}: one row exceeds the {budget}-byte budget")
        if tuples and size + 2 + cost > budget:
            yield from flush()
            tuples, size = [], hlen
        if not tuples:
            lo = row[0]
        tuples.append(lit)
        size += cost + (2 if len(tuples) > 1 else 0)
        hi = row[0]
    if tuples:
        yield from flush()


def fts_phrase(text: str) -> str:
    """One quoted FTS5 phrase, as the Worker passes a query."""
    return '"' + text.replace('"', '""') + '"'


# ---- sizes ------------------------------------------------------------------------------------

FTS_SHADOWS = ("_data", "_idx", "_docsize", "_config", "_content")


def dbstat(path: Path) -> tuple[dict[str, int], list[str]]:
    """Bytes per table and index (pages × page size, dbstat aggregate), and the FTS tables."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page = db.execute("PRAGMA page_size").fetchone()[0]
        stat = {name: pages * page for name, pages in db.execute(
            "SELECT name, count(*) FROM dbstat GROUP BY name ORDER BY count(*) DESC")}
        fts = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")]
        return stat, fts
    finally:
        db.close()


def group_fts(stat: dict[str, int], fts: list[str]) -> dict[str, int]:
    """dbstat bytes by owner: an FTS table's shadow tables (<t>_data, _idx, ...) under
    "<t> (fts)"; tables, UNIQUE autoindexes and named indexes stay separate rows."""
    out: dict[str, int] = defaultdict(int)
    for name, size in stat.items():
        key = name
        for base in fts:
            if any(name == base + s for s in FTS_SHADOWS):
                key = f"{base} (fts)"
        out[key] += size
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def sizes(serving: Path, sidecar: dict) -> dict:
    db = sqlite3.connect(f"file:{serving}?mode=ro", uri=True)
    try:
        def top(sql, n=5):
            return [list(r) for r in db.execute(sql + f" LIMIT {n}")]
        largest = {
            "cell_pages": top("SELECT id, length(CAST(json AS BLOB)) b FROM cell_pages ORDER BY b DESC"),
            "supercells": top("SELECT path, length(CAST(json AS BLOB)) + coalesce(length(CAST(syn AS BLOB)), 0)"
                              " + length(CAST(fhist AS BLOB)) + coalesce(length(CAST(kids AS BLOB)), 0) b"
                              " FROM supercells ORDER BY b DESC"),
            "synapse_traces": top("SELECT pair, length(CAST(json AS BLOB)) b FROM synapse_traces ORDER BY b DESC"),
            "hot_prefixes": top("SELECT prefix, length(cells) + length(sups) + length(live_sups) b"
                                " FROM hot_prefixes ORDER BY b DESC", 3),
            "text_ids": top("SELECT node, length(CAST(body AS BLOB)) b FROM text_ids ORDER BY b DESC", 3),
        }
        folder = {
            "most_direct_cells": top("SELECT path, n_cells, n_sub, length(CAST(json AS BLOB)) FROM supercells"
                                     " ORDER BY n_cells DESC NULLS LAST", 3),
            "largest_subtree": top("SELECT path, n_cells, n_sub, length(CAST(json AS BLOB)) FROM supercells"
                                   " ORDER BY n_sub DESC", 3),
        }
        counts = {
            "cells": db.execute("SELECT count(*) FROM labels").fetchone()[0],
            "supercells": db.execute("SELECT count(*) FROM supercells").fetchone()[0],
            "decl_cells_tauceti": db.execute(
                "SELECT count(*) FROM labels WHERE id LIKE 'cell:decl:TauCeti:%'").fetchone()[0],
        }
    finally:
        db.close()
    stat, fts = dbstat(serving)
    return {"file_bytes": serving.stat().st_size, "dbstat": stat, "by_table": group_fts(stat, fts),
            "rows": {t: v["rows"] for t, v in sidecar["tables"].items()},
            "largest_rows": largest, "largest_folder": folder, "counts": counts}


# ---- the digest (verify-style) ------------------------------------------------------------------

def digest_time(serving: Path, sidecar_path: Path) -> dict:
    import brain_d1_import as bdi
    t0 = time.monotonic()
    local = bdi.open_local(serving, sidecar_path, full_check=True)
    seconds = time.monotonic() - t0
    dataset_id = local.dataset_id
    local.close()
    return {"open_local_full_check_s": round(seconds, 2), "dataset_id": dataset_id}


# ---- the offline import ----------------------------------------------------------------------------

class LocalD1:
    """Executes each `wrangler d1 execute --command` against a SQLite file made from the
    migration, answering Wrangler's --json shape, and records every statement's size."""

    def __init__(self, path: Path, migration: Path) -> None:
        from test_brain_d1_import import split_sql
        self.split_sql = split_sql
        path.unlink(missing_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        self.db.execute("PRAGMA journal_mode = OFF")
        self.db.execute("PRAGMA synchronous = OFF")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(migration.read_text())
        self.calls = 0
        self.sizes: list[int] = []
        self.kinds: dict[str, list[int]] = defaultdict(list)
        self.rows_written = 0
        self.exec_s = 0.0

    def run(self, args, *, cwd, timeout, env):
        from brain_promote_release import RunResult
        args = [str(a) for a in args]
        self.calls += 1
        results = []
        for statement in self.split_sql(args[-1]):
            results.append(self.execute(statement))
        return RunResult(tuple(args), 0, json.dumps(results).encode(), b"")

    def execute(self, statement: str) -> dict:
        size = len(statement.encode())
        self.sizes.append(size)
        self.kinds[statement.split(None, 1)[0].upper()].append(size)
        t0 = time.perf_counter()
        cursor = self.db.execute(statement)
        rows = ([dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
                if cursor.description else [])
        self.exec_s += time.perf_counter() - t0
        written = max(cursor.rowcount, 0)
        self.rows_written += written
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        return {"results": rows, "success": True,
                "meta": {"duration": 0.1, "changes": written, "rows_written": written,
                         "size_after": pages * PAGE_SIZE}}

    def summary(self) -> dict:
        return {"statements": len(self.sizes), "max_bytes": max(self.sizes, default=0),
                "p99_bytes": percentile(self.sizes, 99), "p50_bytes": percentile(self.sizes, 50),
                "total_bytes": sum(self.sizes),
                "over_100kb": sum(1 for s in self.sizes if s > STATEMENT_LIMIT),
                "by_verb": {k: {"n": len(v), "max": max(v), "p99": percentile(v, 99)}
                            for k, v in sorted(self.kinds.items())},
                "sqlite_rows_changed": self.rows_written, "local_exec_s": round(self.exec_s, 1)}


def offline_import(repo: Path, serving: Path, sidecar_path: Path, d1_path: Path,
                   extra_tables: list[tuple[str, list[str], str]] = ()) -> dict:
    """The importer's scratch import into the local stand-in. `extra_tables` (table, columns,
    create SQL) are planned with plan_rows afterwards and executed into the same file."""
    import brain_d1_import as bdi
    import brain_deploy_journal as journal
    fake = LocalD1(d1_path, repo / "wiki" / "migrations-brain" / "0001_brain.sql")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "repo").mkdir(mode=0o700)
        root = journal.initialize_target_receipt_root(work / "receipts", work / "repo",
                                                      "https://example.test")
        records: list[dict] = []
        d1 = bdi.D1(repo=repo, database=bdi.DATABASE, config=work / "wrangler.json", runner=fake,
                    node=Path("node"), wrangler=Path("wrangler.js"), sleep=lambda s: None,
                    log=records.append)
        options = bdi.Options(execute=True)
        importer = bdi.Importer(d1, options=options, selector=lambda: (None, None),
                                receipt_root=root)
        local = bdi.open_local(serving, sidecar_path, full_check=False)
        t0 = time.monotonic()
        with importer.locked():
            report = importer.import_dataset(local, bdi.SCRATCH_PREFIX + local.dataset_id,
                                             "f" * 64)
        wall = time.monotonic() - t0
        local.close()
    result = {"report_keys": sorted(report), "importer_calls": d1.stats.summary(),
              "plan": fake.summary(), "wall_s": round(wall, 1),
              "options": {"statement_bytes": options.statement_bytes,
                          "call_bytes": options.call_bytes,
                          "ranges_per_call": options.ranges_per_call}}
    pages_after_import = fake.db.execute("PRAGMA page_count").fetchone()[0]
    result["footprint_bytes"] = pages_after_import * PAGE_SIZE
    if extra_tables:
        extra = {}
        src = sqlite3.connect(f"file:{serving}?mode=ro", uri=True)
        ds = fake.db.execute("SELECT ds FROM datasets").fetchone()[0]
        for table, columns, create in extra_tables:
            fake.db.executescript(create)
            before = len(fake.sizes)
            t0 = time.monotonic()
            cursor = src.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY seq")
            for statement in plan_rows(table, columns, cursor, options.statement_bytes, ds=ds):
                fake.execute(statement)
            sizes_ = fake.sizes[before:]
            extra[table] = {"statements": len(sizes_), "max_bytes": max(sizes_, default=0),
                            "p99_bytes": percentile(sizes_, 99), "seconds": round(time.monotonic() - t0, 1)}
        src.close()
        result["extra_tables"] = extra
        result["footprint_with_extra_bytes"] = (
            fake.db.execute("PRAGMA page_count").fetchone()[0] * PAGE_SIZE)
    fake.db.close()
    result["dbstat"] = group_fts(*dbstat(d1_path))
    foot = result.get("footprint_with_extra_bytes", result["footprint_bytes"])
    result["projection"] = {"per_dataset_bytes": foot, "x3_bytes": 3 * foot,
                            "x3_share_of_10GB": round(3 * foot / D1_CAP, 4)}
    return result


# ---- queries ---------------------------------------------------------------------------------------

def timed(db: sqlite3.Connection, sql: str, params=(), *, runs: int = 5) -> dict:
    """Wall ms of a query, fetched whole: the first run, then min/median/max of the rest."""
    times, n = [], 0
    for _ in range(runs + 1):
        t0 = time.perf_counter()
        n = len(db.execute(sql, params).fetchall())
        times.append((time.perf_counter() - t0) * 1000)
    rest = times[1:]
    return {"rows": n, "first_ms": round(times[0], 3), "min_ms": round(min(rest), 3),
            "median_ms": round(statistics.median(rest), 3), "max_ms": round(max(rest), 3)}


def name_search(db: sqlite3.Connection, q: str) -> dict:
    """The names tier statements for query q, as section 1's Search describes them."""
    q = q.lower()
    out = {"exact": timed(db, "SELECT owner FROM name_keys WHERE name_lc = ? ORDER BY seq LIMIT 200", (q,))}
    hot = db.execute("SELECT 1 FROM hot_prefixes WHERE prefix = ?", (q,)).fetchone()
    if hot:
        out["prefix_hot_row"] = timed(db, "SELECT cells, sups, live_sups FROM hot_prefixes WHERE prefix = ?", (q,))
        out["prefix_range_count_if_not_hot"] = timed(
            db, "SELECT count(*) FROM name_keys WHERE name_lc > ? AND name_lc < ? || char(1114111)", (q, q))
        out["prefix_range_count_if_not_hot"]["count"] = db.execute(
            "SELECT count(*) FROM name_keys WHERE name_lc > ? AND name_lc < ? || char(1114111)", (q, q)).fetchone()[0]
    else:
        out["prefix_range"] = timed(db, "SELECT seq, owner FROM name_keys WHERE name_lc > ? AND "
                                        "name_lc < ? || char(1114111) ORDER BY name_lc LIMIT 1001", (q, q))
    if len(q) >= 3:
        out["fragment"] = timed(db, "SELECT rowid FROM names WHERE names MATCH ? ORDER BY rowid LIMIT 2000",
                                (fts_phrase(q),))
    out["hot"] = bool(hot)
    return out


FRAGMENTS = ["semigroup", "continuous", "hopf", "eq_of", "mul_comm", "tauceti.alg", "isclosed",
             "lemma", "ring"]
# frequent trigrams in combinations that never sit next to each other in a name
ADVERSARIAL = ["_of_eq_of_le_of_lt", "ationalgebra", "tauceti.mul_one_eq", "le_of_lt_of_le_of_lt_of",
               "ofofofof", "eq_eq_eq_eq", "_iff_iff_", "tionationation"]
TEXT_WORDS = ["function", "group", "space", "map", "set", "ring", "module", "measure"]


def queries(serving: Path, *, name_index: bool, seed: int = 7) -> dict:
    db = sqlite3.connect(f"file:{serving}?mode=ro", uri=True)
    try:
        out: dict = {"sqlite_version": sqlite3.sqlite_version}
        out["prefixes"] = {q: name_search(db, q) for q in ("ta", "tau", "tauceti.", "semig")}
        out["fragments"] = {q: name_search(db, q)["fragment"] for q in FRAGMENTS}
        out["adversarial"] = {q: name_search(db, q)["fragment"] for q in ADVERSARIAL}
        text = "SELECT * FROM (SELECT rowid, bm25(texts) AS s FROM texts WHERE texts MATCH ? LIMIT 2000) " \
               "ORDER BY s LIMIT 50"
        out["text_or_8"] = timed(db, text, (" OR ".join(TEXT_WORDS),))
        out["text_and_8"] = timed(db, text, (" AND ".join(TEXT_WORDS),))
        out["text_or_8_unbounded_bm25"] = timed(
            db, "SELECT rowid, bm25(texts) s FROM texts WHERE texts MATCH ? ORDER BY s LIMIT 50",
            (" OR ".join(TEXT_WORDS),))
        ids = [r[0] for r in db.execute("SELECT id FROM cell_pages")]
        rng = random.Random(seed)
        sample = rng.sample(ids, min(1000, len(ids)))
        per = []
        for cid in sample:
            t0 = time.perf_counter()
            db.execute("SELECT json FROM cell_pages WHERE id = ?", (cid,)).fetchone()
            per.append((time.perf_counter() - t0) * 1000)
        out["cell_page_1000_random"] = {"mean_ms": round(statistics.mean(per), 4),
                                        "p99_ms": round(sorted(per)[int(len(per) * .99) - 1], 4),
                                        "max_ms": round(max(per), 4)}
        organs = [r[0] for r in db.execute("SELECT key FROM aliases WHERE kind = 'organ'")]
        sample = rng.sample(organs, min(1000, len(organs)))
        per = []
        alias = ("SELECT kind, owner FROM aliases WHERE (kind = 'organ' AND key = ?1) OR "
                 "(kind = 'decl' AND key = ?2) OR (?3 = 1 AND kind = 'slug' AND key = ?1)")
        for key in sample:
            bare = key.split(":", 2)[-1]
            t0 = time.perf_counter()
            db.execute(alias, (key, bare, 1)).fetchall()
            per.append((time.perf_counter() - t0) * 1000)
        out["alias_1000_random"] = {"mean_ms": round(statistics.mean(per), 4),
                                    "p99_ms": round(sorted(per)[int(len(per) * .99) - 1], 4),
                                    "max_ms": round(max(per), 4)}
        if name_index:
            ni: dict = {}
            for name in ("TauCeti.Algebra.HopfAlgebra.antipode", "Nat.succ_le_iff", "mul_comm",
                         "Not.A.Name"):
                ni[f"exact:{name}"] = timed(db, "SELECT name, library, module, doc_link FROM decl_names "
                                                "WHERE name = ?", (name,))
            for p in ("Ta", "Tau", "TauCeti.", "CategoryTheory.Functor.", "Nat.", "mul_"):
                ni[f"prefix:{p}"] = timed(db, "SELECT name, library, module, doc_link FROM decl_names "
                                              "WHERE name >= ? AND name < ? || char(1114111) "
                                              "ORDER BY name LIMIT 100", (p, p))
            for f in ("Semigroup", "mul_comm", "eq_of_", "antipode", "HopfAlg") + tuple(ADVERSARIAL[:3]):
                ni[f"fragment:{f}"] = timed(db, "SELECT rowid FROM decl_names_fts WHERE decl_names_fts "
                                                "MATCH ? ORDER BY rowid LIMIT 200", (fts_phrase(f),))
            out["name_index"] = ni
        return out
    finally:
        db.close()


def run(ctx) -> None:
    repo = Path(__file__).resolve().parents[2]
    sidecar = json.loads(ctx.sidecar.read_text())
    result: dict = {"scope": ctx.scope}
    t0 = time.monotonic()
    result["sizes"] = sizes(ctx.serving, sidecar)
    result["digest"] = digest_time(ctx.serving, ctx.sidecar)
    name_index = ctx.scope == "B" and sqlite3.connect(ctx.serving).execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'decl_names'").fetchone() is not None
    result["queries"] = queries(ctx.serving, name_index=name_index)
    extra = []
    if name_index:
        extra.append(("decl_names", ["seq", "name", "library", "module", "doc_link"],
                      "CREATE TABLE decl_names(ds INTEGER NOT NULL, seq INTEGER NOT NULL, "
                      "name TEXT NOT NULL, library TEXT NOT NULL, module TEXT NOT NULL, "
                      "doc_link TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, name, library));"))
    result["import"] = offline_import(repo, ctx.serving, ctx.sidecar, ctx.dir / "d1-local.sqlite3",
                                      extra)
    if name_index:
        result["import"]["name_index_fts_note"] = (
            "decl_names_fts is not in the D1 stand-in; its local trigram index bytes are in "
            "sizes.by_table and are added in projection_with_name_fts")
        fts = result["sizes"]["by_table"].get("decl_names_fts (fts)", 0)
        foot = result["import"]["projection"]["per_dataset_bytes"] + fts
        result["import"]["projection_with_name_fts"] = {
            "per_dataset_bytes": foot, "x3_bytes": 3 * foot, "x3_share_of_10GB": round(3 * foot / D1_CAP, 4)}
    result["seconds"] = round(time.monotonic() - t0, 1)
    (ctx.dir / "analysis.json").write_text(json.dumps(result, indent=1) + "\n")
    os.chmod(ctx.dir / "analysis.json", 0o644)
