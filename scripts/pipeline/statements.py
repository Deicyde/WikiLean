#!/usr/bin/env python3
"""Stage 4b: a sealed statement set read back into the shape the cut hands on.

docs/STATEMENTS.md. The inverse of :mod:`export_statements`: a sealed
``wikilean.statement-set/1`` in, and the ``{source_id: [passage]}`` of
:func:`cut.frozen_inventory` out — so **phase 2 plans from a published statement set
exactly as it plans from a harvested cut**, and nothing downstream of the planner knows
which it was handed.

Phase 2 does not re-cut. It reads the sealed statements and, against the pinned
revision's own render, **recomputes each ``span_sha256`` and each ``passage_key`` and
refuses the read on any difference** (:func:`frozen_inventory`). The keys therefore
cannot drift between the two phases, and a community tag written on a statement in phase
1 is the row the pipeline's location fills in in phase 2 (docs/ARTICLES.md 10a).

``text_span``, the reading-copy span a worker is told to read at, is **not** sealed: it
is a fact about the local render, recomputed here from the source's ``layer_map.json``
exactly as :func:`cut.frozen_inventory` recomputes it.

Usage::

    python3 scripts/pipeline/statements.py --statement-set <dir> [--sources <dir>]
    python3 scripts/pipeline/statements.py --statement-set <dir> --verify-only

Exit codes: 0 read, 2 the set was refused (the reasons on stderr), 3 the reader failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import keys  # noqa: E402
import readback  # noqa: E402
import records as records_mod  # noqa: E402
import sources as sources_mod  # noqa: E402
import vocab  # noqa: E402


class StatementSetProblem(ValueError):
    """A sealed statement set this pipeline refuses to read, with every reason it found."""


@dataclass
class StatementSet:
    """A sealed statement set, read back."""

    path: Path
    seal: dict
    seal_sha256: str
    #: ``{source_id: the sources.jsonl row}``, each exactly :data:`vocab.SOURCE_KEYS`
    sources: dict[str, dict] = field(default_factory=dict)
    #: ``{source_id: [statement]}``, each source's statements in span then item order
    statements: dict[str, list[dict]] = field(default_factory=dict)
    #: The rendered sources :func:`frozen_inventory` checked every span and quote
    #: against, or None: a set read from its bytes alone is read, not verified.
    verified_against: Path | None = None

    @property
    def set_id(self) -> str:
        return str(self.seal["set_id"])

    def cuts(self) -> dict[tuple[str, str], records_mod.Cut]:
        """``{(source_id, item): Cut}`` — what the layer gave each passage, as sealed.

        This is what stage 6 takes instead of an outcome row's own ``cut``: the quote and
        the span's sha256 are the statement set's, and a row that states another is
        refused by the export.

        A quote is a claim about the article's text and the sealed bytes cannot settle
        it, so this refuses a set no render has been checked against: run
        :func:`frozen_inventory` with the sourceset's rendered sources first.
        """
        if self.verified_against is None:
            raise StatementSetProblem(
                f"{self.set_id}: the set's quotes have not been checked against a render, and a "
                "record is written with one; call frozen_inventory(set, <sources dir>) first")
        return {(sid, row["item"]): records_mod.Cut(dict(row["quote"]), row["span_sha256"])
                for sid, rows in self.statements.items() for row in rows}


# ---- the seal ----------------------------------------------------------------------------


def _seal_problems(seal: Any) -> list[str]:
    """Why ``SEAL.json`` is not a statement set's seal (docs/STATEMENTS.md section 2)."""
    if not isinstance(seal, dict):
        return ["SEAL.json: not an object"]
    found: list[str] = []
    if seal.get("schema") != vocab.STATEMENT_SET_SCHEMA:
        found.append(f"SEAL.json: schema {seal.get('schema')!r} is not "
                     f"{vocab.STATEMENT_SET_SCHEMA!r}")
    missing = [key for key in vocab.STATEMENT_SEAL_KEYS if key not in seal]
    stray = sorted(set(seal) - set(vocab.STATEMENT_SEAL_KEYS))
    if missing:
        found.append(f"SEAL.json: no {', '.join(missing)}")
    if stray:
        found.append(f"SEAL.json: a statement set's seal carries {len(vocab.STATEMENT_SEAL_KEYS)} "
                     f"keys and no more ({', '.join(stray)})")
    if "set_id" in seal and not (isinstance(seal["set_id"], str)
                                 and vocab.TOKEN_RE.match(seal["set_id"])):
        found.append(f"SEAL.json: set_id is {vocab.TOKEN_RULE}")
    if "campaign" in seal and not (isinstance(seal["campaign"], str)
                                   and vocab.CAMPAIGN_RE.match(seal["campaign"])):
        found.append("SEAL.json: campaign is a campaign id, or run:<run-id>[+<challenge-run-id>]")
    if "producer_commit" in seal and not (isinstance(seal["producer_commit"], str)
                                          and vocab.HEX40.match(seal["producer_commit"])):
        found.append("SEAL.json: producer_commit is 40 lowercase hex")
    if "created" in seal and not (isinstance(seal["created"], str)
                                  and vocab.CREATED_RE.match(seal["created"])):
        found.append("SEAL.json: created is an ISO 8601 UTC time (YYYY-MM-DDThh:mm:ssZ)")
    sourceset = seal.get("sourceset")
    if "sourceset" in seal:
        if not isinstance(sourceset, dict) or set(sourceset) != set(vocab.SEAL_SOURCESET_KEYS):
            found.append("SEAL.json: sourceset is {id, manifest_sha256}")
        else:
            if not (isinstance(sourceset["id"], str) and vocab.TOKEN_RE.match(sourceset["id"])):
                found.append(f"SEAL.json: sourceset.id is {vocab.TOKEN_RULE}")
            if not (isinstance(sourceset["manifest_sha256"], str)
                    and vocab.HEX64.match(sourceset["manifest_sha256"])):
                found.append("SEAL.json: sourceset.manifest_sha256 is 64 lowercase hex")
    rules = seal.get("cut_rules")
    if "cut_rules" in seal:
        if not isinstance(rules, dict) or set(rules) != set(vocab.STATEMENT_CUT_RULE_KEYS):
            found.append(f"SEAL.json: cut_rules is "
                         f"{{{', '.join(vocab.STATEMENT_CUT_RULE_KEYS)}}}")
        else:
            for name in vocab.STATEMENT_CUT_RULE_KEYS:
                if type(rules[name]) is not int or rules[name] < 1:
                    found.append(f"SEAL.json: cut_rules.{name} is the rule number the cut ran "
                                 f"under, an integer of 1 or more, not {rules[name]!r}")
    if "files" in seal and (not isinstance(seal["files"], dict) or not seal["files"]):
        found.append("SEAL.json: files maps each relative path to its sha256")
    return found


# ---- the sources and the statements ----------------------------------------------------------


def _jsonl(text: str, path: str, found: list[str]) -> list[tuple[int, Any]]:
    """``[(line number, value)]`` of a canonical JSON-lines file, refusing by line.

    Lines end at ``\\n`` only: JSON holds a U+2028 or U+2029 unescaped inside a string,
    which ``str.splitlines`` would cut, and the writer writes them so.
    """
    out: list[tuple[int, Any]] = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            if line:
                found.append(f"{path}:{number}: a blank line")
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            found.append(f"{path}:{number}: not JSON ({exc})")
            continue
        if not isinstance(value, dict):
            found.append(f"{path}:{number}: not an object")
            continue
        out.append((number, value))
    return out


def _source_rows(text: str, found: list[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    pages: dict[int, str] = {}
    for number, row in _jsonl(text, "sources.jsonl", found):
        at = f"sources.jsonl:{number}"
        stray = sorted(set(row) - set(vocab.SOURCE_KEYS))
        missing = [key for key in vocab.SOURCE_KEYS if key not in row]
        if stray or missing:
            found.append(f"{at}: a source row is exactly {{{', '.join(vocab.SOURCE_KEYS)}}}"
                         + (f"; stray {', '.join(stray)}" if stray else "")
                         + (f"; no {', '.join(missing)}" if missing else ""))
            continue
        try:
            sources_mod.source_record(row)       # the family, the licence and the identity
        except sources_mod.SourceProblem as exc:
            found.append(f"{at}: {exc}")
            continue
        source_id = str(row["source_id"])
        if source_id in rows:
            found.append(f"{at}: {source_id} appears twice")
            continue
        other = pages.setdefault(int(row["pageid"]), source_id)
        if other != source_id:
            found.append(f"{at}: page {row['pageid']} is already {other}; a set holds one "
                         "revision of a page")
            continue
        for name in ("html_sha256", "layer_sha256"):
            if not vocab.HEX64.match(str(row.get(name) or "")):
                found.append(f"{at}: {name} is not 64 lowercase hex")
        if not vocab.RENDERER_RE.match(str(row.get("renderer") or "")):
            found.append(f"{at}: renderer {row.get('renderer')!r} is no renderer id")
        rows[source_id] = {key: row[key] for key in vocab.SOURCE_KEYS}
    return rows


def _free_text(at: str, what: str, text: str, layer: str) -> list[str]:
    """The quote cap gone round the back: a free-text field quoting past it."""
    try:
        records_mod.check_free_text(at, what, text, layer)
    except records_mod.RecordProblem as exc:
        return [str(exc)]
    return []


def _unencodable(value: Any, where: str = "") -> list[str]:
    """The names of the fields of ``value`` holding text UTF-8 cannot carry."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return [where or "the statement"]
        return []
    if isinstance(value, Mapping):
        return [name for key, inner in value.items()
                for name in _unencodable(inner, f"{where}.{key}" if where else str(key))]
    if isinstance(value, list):
        return [name for n, inner in enumerate(value)
                for name in _unencodable(inner, f"{where}[{n}]")]
    return []


def statement_problems(row: Any, pageid: int, at: str, layer: str | None = None) -> list[str]:
    """Why one statement is not one: **the** rule set, the writer's and the reader's.

    :mod:`export_statements` projects a harvested passage into the sealed shape and then
    runs this over it, and the reader runs it over the sealed bytes, so neither side can
    hold a statement to a rule the other does not. Every reason is reported at once.

    ``pageid`` keys the passage, so ``passage_key`` is recomputed here rather than read.
    ``layer`` — the pinned text layer, when the caller holds it — adds the three rules
    that cannot be checked from the bytes alone: the quote is the one text at the span,
    and neither the restatement nor a conjunct carries the source past the quote cap.
    """
    if not isinstance(row, Mapping):
        return [f"{at}: a statement is an object, not {type(row).__name__}"]
    found: list[str] = []
    stray = sorted(set(row) - set(vocab.STATEMENT_KEYS) - set(vocab.STATEMENT_OPTIONAL))
    missing = [key for key in vocab.STATEMENT_KEYS if key not in row]
    if stray:
        found.append(f"{at}: a statement carries {', '.join(vocab.STATEMENT_KEYS)} and, on a "
                     f"compound, {' and '.join(vocab.STATEMENT_OPTIONAL)}; stray "
                     f"{', '.join(stray)}")
    if missing:
        found.append(f"{at}: no {', '.join(missing)}")
    if missing:
        return found
    # A lone surrogate is a string Python holds and UTF-8 cannot carry: it reaches here
    # only as a \udXXX escape in a hand-made line, and everything written from a statement
    # is UTF-8 (a record's canonical JSON, the store's view model). Named here rather than
    # raised as a UnicodeEncodeError by whichever writer meets it first.
    unencodable = sorted(_unencodable(row))
    if unencodable:
        found.append(f"{at}: {', '.join(unencodable)} holds a character UTF-8 cannot carry "
                     "(a lone surrogate), and everything written from a statement is UTF-8")
    item = row["item"]
    if not (isinstance(item, str) and vocab.ITEM_RE.match(item)):
        found.append(f"{at}: item {item!r} is no 'In-text #<8 to 16 hex>'")
    span = row["span"]
    if (not isinstance(span, list) or len(span) != 2
            or not all(type(offset) is int for offset in span) or not 0 <= span[0] < span[1]):
        found.append(f"{at}: span {span!r} is no [start, end] with 0 <= start < end")
    elif layer is not None and span[1] > len(layer):
        found.append(f"{at}: the span {span} leaves the pinned layer (0..{len(layer)})")
    for name in ("span_sha256", "passage_key"):
        if not (isinstance(row[name], str) and vocab.HEX64.match(row[name])):
            found.append(f"{at}: {name} is not 64 lowercase hex")
    quote = row["quote"]
    good_span = not found or all("span " not in message for message in found)
    if (not isinstance(quote, dict) or set(quote) != {"exact", "prefix", "suffix"}
            or not all(isinstance(quote.get(name), str) for name in quote)
            or not quote.get("exact")):
        found.append(f"{at}: quote is {{exact, prefix, suffix}}, the exact part non-empty")
    elif len(quote["exact"].split()) > vocab.QUOTE_MAX_WORDS:
        found.append(f"{at}: the quote is {len(quote['exact'].split())} words, over the cap of "
                     f"{vocab.QUOTE_MAX_WORDS}")
    elif any(len(quote[name]) > vocab.AFFIX_MAX_CHARS for name in ("prefix", "suffix")):
        found.append(f"{at}: a quote affix is over the cap of {vocab.AFFIX_MAX_CHARS} characters")
    elif good_span and span[0] + len(quote["exact"]) > span[1]:
        found.append(f"{at}: the quote runs past the end of the span")
    elif good_span and layer is not None and records_mod.find_quote(
            layer, quote["exact"], quote["prefix"], quote["suffix"]) != [span[0]]:
        found.append(f"{at}: the quote is not the one text at {span[0]} on the pinned layer "
                     "with its prefix and suffix")
    if not (isinstance(row["section"], list)
            and all(isinstance(head, str) and head.strip() for head in row["section"])):
        found.append(f"{at}: section is the heading path, [] in the lead")
    if row["fragment"] is not None and not (isinstance(row["fragment"], str)
                                            and row["fragment"].strip()
                                            and row["fragment"] == row["fragment"].strip()):
        found.append(f"{at}: fragment {row['fragment']!r} is the section's fragment, or null — "
                     "never an empty or padded string")
    if not (isinstance(row["head_words"], int) and not isinstance(row["head_words"], bool)
            and row["head_words"] >= 1):
        found.append(f"{at}: head_words is how many words the head rule hashed")
    for name, allowed in (("passage_kind", vocab.KINDS), ("prominence", vocab.PROMINENCES),
                          ("scope", vocab.SCOPES), ("finder", vocab.FINDERS)):
        if not vocab.member(row[name], allowed):
            found.append(f"{at}: {name} {row[name]!r} is not one of {', '.join(sorted(allowed))}")
    if not isinstance(row["statement"], str):
        found.append(f"{at}: statement is the worker's one-sentence restatement")
    elif row["scope"] == vocab.SCOPE_FORMAL_MATH and not row["statement"].strip():
        found.append(f"{at}: a formal_math statement carries its restatement")
    elif len(row["statement"]) > vocab.MAX_REASON_CHARS:
        found.append(f"{at}: the statement is {len(row['statement'])} characters, over the cap "
                     f"of {vocab.MAX_REASON_CHARS}")
    elif layer is not None:
        found.extend(_free_text(at, "the statement", row["statement"], layer))
    if row["scope"] != vocab.SCOPE_FORMAL_MATH and row["prominence"] != vocab.PROMINENCE_SUPPORTING:
        found.append(f"{at}: a {row['scope']!r} passage is always supporting")
    has = [name for name in vocab.STATEMENT_OPTIONAL if name in row]
    if has and len(has) != len(vocab.STATEMENT_OPTIONAL):
        found.append(f"{at}: {' and '.join(vocab.STATEMENT_OPTIONAL)} ride together")
    elif has:
        if row["scope"] != vocab.SCOPE_FORMAL_MATH:
            found.append(f"{at}: conjuncts on a {row['scope']!r} passage")
        if not vocab.member(row["shape"], vocab.COMPOUND_SHAPES):
            found.append(f"{at}: shape {row['shape']!r} is not one of "
                         f"{', '.join(vocab.COMPOUND_SHAPE_ORDER)}")
        conjuncts = row["conjuncts"]
        if not (isinstance(conjuncts, list)
                and all(isinstance(c, dict) and set(c) == {"id", "text"}
                        and isinstance(c["id"], str) and c["id"].strip()
                        and c["id"] == c["id"].strip()
                        and isinstance(c["text"], str) and c["text"].strip()
                        for c in conjuncts)):
            found.append(f"{at}: a conjunct is {{id, text}} and nothing else — it carries no "
                         "part until a locate run gives it one — the id a bare token and the "
                         "text non-empty")
        elif not vocab.MIN_CONJUNCTS <= len(conjuncts) <= vocab.MAX_CONJUNCTS:
            found.append(f"{at}: {len(conjuncts)} conjuncts, and a compound has "
                         f"{vocab.MIN_CONJUNCTS} to {vocab.MAX_CONJUNCTS}")
        elif len({c["id"] for c in conjuncts}) != len(conjuncts):
            found.append(f"{at}: two conjuncts share an id")
        elif (vocab.SHAPE_CONJUNCT_COUNTS.get(row["shape"]) not in (None, len(conjuncts))):
            found.append(f"{at}: shape {row['shape']!r} has exactly "
                         f"{vocab.SHAPE_CONJUNCT_COUNTS[row['shape']]} conjuncts")
        else:
            for conjunct in conjuncts:
                if len(conjunct["text"]) > vocab.MAX_CONJUNCT_CHARS:
                    found.append(f"{at}: conjunct {conjunct['id']} is "
                                 f"{len(conjunct['text'])} characters, over the cap of "
                                 f"{vocab.MAX_CONJUNCT_CHARS}")
                elif layer is not None:
                    found.extend(_free_text(at, f"conjunct {conjunct['id']}'s text",
                                            conjunct["text"], layer))
    if found:
        return found
    try:
        computed = keys.passage_key(pageid, row["span_sha256"])
    except keys.KeyProblem as exc:
        return [f"{at}: {exc}"]
    if computed != row["passage_key"]:
        found.append(f"{at}: passage_key {row['passage_key'][:12]} is not "
                     f'sha256("{pageid}\\n<span_sha256>")')
    return found


def read_set(set_dir: Path) -> StatementSet:
    """Read a sealed statement set, refusing it with every reason the reader found."""
    set_dir = Path(set_dir)
    found = readback.verify_set(set_dir)
    if found:
        raise StatementSetProblem("\n".join(found))
    seal = json.loads((set_dir / "SEAL.json").read_text(encoding="utf-8"))
    found = _seal_problems(seal)
    if found:
        raise StatementSetProblem("\n".join(found))
    named = set(seal["files"])
    if "sources.jsonl" not in named:
        raise StatementSetProblem("the seal names no sources.jsonl")
    sources = _source_rows((set_dir / "sources.jsonl").read_text(encoding="utf-8"), found)
    wanted = {"sources.jsonl", *(f"statements/{sid}.jsonl" for sid in sources)}
    for stray in sorted(named - wanted):
        found.append(f"{stray}: a statement set holds sources.jsonl and statements/ and "
                     "nothing else")
    for missing in sorted(wanted - named):
        found.append(f"{missing}: a listed source has no statements file")
    statements: dict[str, list[dict]] = {}
    for source_id, source in sorted(sources.items()):
        path = f"statements/{source_id}.jsonl"
        if path not in named:
            continue
        rows: list[dict] = []
        seen_items: set[str] = set()
        seen_keys: set[str] = set()
        subject: str | None = None
        for number, row in _jsonl((set_dir / path).read_text(encoding="utf-8"), path, found):
            at = f"{path}:{number}"
            problems = statement_problems(row, int(source["pageid"]), at)
            if problems:
                found.extend(problems)
                continue
            if row["item"] in seen_items or row["passage_key"] in seen_keys:
                found.append(f"{at}: {row['item']} is a second statement with one item or one "
                             "span text, which no key could tell apart")
                continue
            if row["prominence"] == vocab.PROMINENCE_SUBJECT:
                if subject is not None:
                    found.append(f"{at}: a second subject statement beside {subject}")
                    continue
                subject = row["item"]
            seen_items.add(row["item"])
            seen_keys.add(row["passage_key"])
            rows.append(row)
        order = sorted(rows, key=lambda r: (r["span"][0], r["item"]))
        if order != rows:
            found.append(f"{path}: the statements are written by span start, then item")
        for before, after in zip(order, order[1:]):
            if after["span"][0] < before["span"][1]:
                found.append(f"{path}: {after['item']} at {after['span']} shares a character "
                             f"with {before['item']} at {before['span']}")
        if not order:
            found.append(f"{path}: a listed source has no statement; the writer leaves a "
                         "source with nothing to seal out of sources.jsonl")
        statements[source_id] = order
    if found:
        raise StatementSetProblem("\n".join(found))
    return StatementSet(path=set_dir, seal=seal,
                        seal_sha256=keys.sha256_file(set_dir / "SEAL.json"),
                        sources=sources, statements=statements)


# ---- the cut, as the later stages read it ------------------------------------------------


def manifest_problems(st: StatementSet, entries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Why a sourceset's entries are not the revisions the set was cut from.

    Phase 2 re-renders the **pinned** revision, so the sourceset must pin the very bytes
    the cut read: the same page, revision, renderer and both shas. A sourceset that has
    moved on is refused by name rather than planned against.
    """
    found: list[str] = []
    for source_id, source in sorted(st.sources.items()):
        entry = entries.get(source_id)
        if entry is None:
            found.append(f"{source_id}: the sourceset pins no entry for it, and phase 2 re-renders "
                         "the revision the cut was made against")
            continue
        for name in ("pageid", "revision", "html_sha256", "layer_sha256", "renderer"):
            if entry.get(name) != source[name]:
                found.append(f"{source_id}: the set was cut from {name} {source[name]!r} and the "
                             f"sourceset pins {entry.get(name)!r}")
    return found


def frozen_inventory(st: StatementSet | Path, sources_dir: Path | None = None
                     ) -> dict[str, list[dict]]:
    """``{source_id: [passage]}`` of a sealed set, in the shape :mod:`cut` returns.

    With ``sources_dir`` — the sourceset's rendered sources — the set is **checked
    against the render**: the layer must hash to the source's ``layer_sha256``, each
    statement's ``span_sha256`` recomputed from that layer must be the sealed one, and
    the whole rule set runs again with the layer in hand, which adds the three rules the
    sealed bytes cannot carry (:func:`statement_problems`: the quote is the one text at
    its span, and neither the restatement nor a conjunct quotes the source past the
    cap). Each passage then also gets its reading-copy span (``text_span``), mapped
    through the renderer's ``layer_map.json``, which is what tells a worker where to
    read, and the set is marked verified, which is what :meth:`StatementSet.cuts`
    requires.

    Without ``sources_dir`` the sealed statements are returned as they stand. That is a
    read, not a verification: a caller that will act on the quotes passes the render.
    """
    if not isinstance(st, StatementSet):
        st = read_set(Path(st))
    out = {sid: [dict(row) for row in rows] for sid, rows in st.statements.items()}
    if sources_dir is None:
        return out
    import render_web  # noqa: PLC0415 — only a read that maps spans needs it

    found: list[str] = []
    for sid, rows in sorted(out.items()):
        directory = Path(sources_dir) / sid
        path = directory / "layer.txt"
        if not path.is_file():
            found.append(f"{path} is missing: phase 2 re-renders the pinned revision and checks "
                         "every span against it")
            continue
        layer = path.read_text(encoding="utf-8")
        sealed = str(st.sources[sid]["layer_sha256"])
        if keys.sha256_text(layer) != sealed:
            found.append(f"{sid}: the rendered layer hashes to {keys.sha256_text(layer)[:12]}… and "
                         f"the set was cut from {sealed[:12]}…")
            continue
        if not (directory / "layer_map.json").is_file():
            found.append(f"{directory}/layer_map.json is missing: a locate plan maps each "
                         "statement's span onto the reading copy with it")
            continue
        segments = render_web.LayerMap.read(directory).segments
        pageid = int(st.sources[sid]["pageid"])
        for row in rows:
            at = f"{sid} {row['item']}"
            try:
                span_sha = keys.span_sha256(layer, row["span"])
            except keys.KeyProblem as exc:
                found.append(f"{at}: {exc}")
                continue
            if span_sha != row["span_sha256"]:
                found.append(f"{at}: the span {row['span']} on the pinned layer hashes to "
                             f"{span_sha[:12]}… and the set sealed "
                             f"{row['span_sha256'][:12]}…; the cut and the render disagree")
                continue
            problems = statement_problems(row, pageid, at, layer=layer)
            if problems:
                found.extend(problems)
                continue
            row["text_span"] = render_web.text_span(segments, row["span"])
    if found:
        raise StatementSetProblem("\n".join(found))
    st.verified_against = Path(sources_dir)
    return out


def counts(st: StatementSet) -> dict:
    """What a statement set holds: sources, statements, and the statements by facet."""
    rows = [row for source in st.statements.values() for row in source]
    by = lambda key: {  # noqa: E731
        value: sum(1 for row in rows if row.get(key) == value)
        for value in sorted({str(row.get(key)) for row in rows})}
    return {"sources": len(st.statements), "statements": len(rows),
            "by_scope": by("scope"), "by_prominence": by("prominence"),
            "by_kind": by("passage_kind"), "by_finder": by("finder"),
            "located": sum(1 for row in rows if row.get("scope") in vocab.LOCATED_SCOPES)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--statement-set", required=True, type=Path)
    parser.add_argument("--sources", type=Path,
                        help="the sourceset's rendered sources: the whole check — every span "
                             "and every quote against the pinned render, and the reading-copy "
                             "spans. Required unless --verify-only")
    parser.add_argument("--verify-only", action="store_true",
                        help="check the seal and the sealed bytes alone. The three rules that "
                             "need the article's text — the quote is the one text at its span, "
                             "and the restatement and the conjuncts stay inside the quote cap — "
                             "are NOT checked: pass --sources for those")
    args = parser.parse_args(argv)
    if args.sources is None and not args.verify_only:
        parser.error("--sources names the sourceset's rendered sources, which every span and "
                     "quote is checked against; pass --verify-only to check the bytes alone")
    try:
        st = read_set(args.statement_set)
        if not args.verify_only:
            frozen_inventory(st, args.sources)
    except StatementSetProblem as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"the read failed: {exc!r}", file=sys.stderr)
        return 3
    summary = counts(st)
    print(f"{st.set_id} [{vocab.STATEMENT_SET_SCHEMA}]: {summary['sources']} source(s), "
          f"{summary['statements']} statement(s), {summary['located']} formal_math")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
