#!/usr/bin/env python3
"""Stage 6b: the sealed **statement set**, written.

docs/STATEMENTS.md. A harvested cut (stage 9b) in; a directory out::

    <set>/SEAL.json                      the pins, the cut rules, and every file's sha256
    <set>/sources.jsonl                  one row per source: ids, revision, title, QID, shas
    <set>/statements/<source-id>.jsonl   one statement per line

A statement set is a **sibling** of the record set, not a profile of it: it states what
the cut found and nothing about Lean. So it carries no ``universe.json`` (nothing was
searched), no receipts, no ``challenge`` value and no status — the three things a
``cf-locate-recordset/{1,2}`` requires of every record and a cut cannot honestly give
(docs/STATEMENTS.md section 1). Nothing shared with formal-companion changes.

Everything else is the record set's, verbatim: :mod:`keys`' key recipes, the one
canonical JSON spelling, the seal's grammar and token rules, the 12-word quote cap and
the affix cap, and the cut rules gate 7 holds a document to. ``passage_key`` is
recomputed here from the page id and the span's sha256 on the pinned layer, so the key a
community tag is written on in phase 1 is the key the pipeline's location carries in
phase 2 (docs/ARTICLES.md 10a, "the key that makes it work").

**The set is a function of its inputs.** The same cut, sources and pins write
byte-identical files: canonical JSON throughout, statements by span start then item,
sources by source id, files sorted in the seal.

What it refuses, statement by statement, naming each one in the report and exiting
non-zero after writing the rest: a passage with no item the head rule could have given,
a span outside the layer, a quote that is not at the span's start or not unique on the
layer, a quote or an affix over the cap, a statement, a conjunct or a quote that carries
the source past the quote cap, a kind, a prominence, a scope or a finder outside the
vocabulary, a compound with no shape (or a shape with no conjuncts), conjuncts on a
passage that is not ``formal_math``, a passage that is not ``formal_math`` and not
``supporting``, two passages of one source with one item, one span text or one shared
character, and a second ``subject`` passage. What it refuses whole: a source whose family
may not be quoted, a source the sourceset does not pin, a source whose rendered layer is
not the one the cut was made against, and a target directory that is not empty or an
earlier statement set.

Usage::

    python3 scripts/pipeline/export_statements.py --inventory <harvested cut dir> \\
        --sourceset <dir> [--sourceset-id <id>] --out <dir> --set-id <token> \\
        --campaign <token> --producer-commit <40 hex> --created <ISO-Z> \\
        [--span-end-rule 3] [--statement-cut-rule 4] [--source <id> ...] [--report <file>]

Exit codes: 0 every statement exported, 1 some were refused (the set holds the rest),
2 the inputs were refused before anything was written, 3 the writer failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "run", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import export_records  # noqa: E402
import keys  # noqa: E402
import records as records_mod  # noqa: E402
import sources as sources_mod  # noqa: E402
import statements as statements_mod  # noqa: E402
import vocab  # noqa: E402

EXPORT_REPORT_SCHEMA = "wikilean.pipeline.export-statements/1"
#: The cut rules this pipeline runs under today (``run.plan``'s defaults): span-end rule
#: 3 and statement-cut rule 4, the rule 10 of issue #172. A set sealed from an older run
#: names that run's rules instead — the seal records them, it does not impose them.
DEFAULT_SPAN_END_RULE = 3
DEFAULT_STATEMENT_CUT_RULE = 4


class StatementExportProblem(ValueError):
    """An input the statement export refuses before it writes anything."""


# ---- one statement ------------------------------------------------------------------------


def _span(value: Any, item: str) -> list[int] | Any:
    """The span as the sealed shape carries it, or the value itself for the rule set."""
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(offset) is int for offset in value)):
        return [int(value[0]), int(value[1])]
    return value


def statement(source: Mapping[str, Any], passage: Mapping[str, Any], layer: str) -> dict:
    """One sealed statement, every value checked and both keys recomputed.

    ``source`` is the sourceset entry (its ``pageid`` keys the passage), ``passage`` is
    the harvested cut's row and ``layer`` is the pinned text layer the span is offsets
    on. A passage that states a ``passage_key`` or a ``span_sha256`` of its own must
    state the one the layer gives, and is refused by name otherwise.

    The projection is here and **the rules are not**: the sealed shape is built from the
    passage's own values, the keys and the quote are recomputed from the layer, and
    :func:`statements.statement_problems` — the one rule set, which the reader runs over
    the sealed bytes — judges the result. So the writer cannot hold a statement to a rule
    the reader does not, nor the other way about.

    **The quote is derived, not copied.** A cut passage carries the quote the worker
    found it at, which gate rule 2 holds to being the one text at the span and nothing
    more; the quote a *record* carries is the format's capped head
    (:func:`records.cut_passage`: the span's first 12 words, made unique by as little
    context as it takes). Phase 1 publishes that one, so the passage a reader tags and
    the location the pipeline later writes show the same words. The passage's own quote
    is still checked — it must be the one text at the span — and then set aside.
    """
    if not isinstance(passage, Mapping):
        raise records_mod.RecordProblem(
            f"a cut passage is an object, not {type(passage).__name__}")
    item = str(passage.get("item") or "")
    span = _span(passage.get("span"), item)
    try:
        cut = records_mod.cut_passage(layer, span if isinstance(span, list) else [])
    except records_mod.RecordProblem as exc:
        raise records_mod.RecordProblem(f"{item or '(no item)'}: {exc}") from None
    span_sha = cut.span_sha256
    given = passage.get("quote")
    if not (isinstance(given, Mapping) and isinstance(given.get("exact"), str)
            and given["exact"]):
        raise records_mod.RecordProblem(
            f"{item or '(no item)'}: a cut passage carries the quote it was cut at "
            "(gate rule 2), and this one carries none")
    if records_mod.find_quote(layer, given["exact"], str(given.get("prefix") or ""),
                              str(given.get("suffix") or "")) != [span[0]]:
        raise records_mod.RecordProblem(
            f"{item}: the cut's own quote is not the one text at {span[0]} on the pinned layer "
            "with its prefix and suffix")
    if span[0] + len(given["exact"]) > span[1]:
        # gate rule 2's third check, kept here because the sealed quote is derived: a
        # span that does not hold the words it was cut at would seal as a statement of
        # the few characters the span does hold.
        raise records_mod.RecordProblem(
            f"{item}: the cut's own quote runs past the end of the span")
    stated_sha = passage.get("span_sha256")
    if stated_sha is not None and stated_sha != span_sha:
        raise records_mod.RecordProblem(
            f"{item}: the passage states span_sha256 {str(stated_sha)[:12]}, and its span on the "
            "pinned layer hashes to another")
    try:
        passage_key = keys.passage_key(source.get("pageid"), span_sha)
    except keys.KeyProblem as exc:
        raise records_mod.RecordProblem(f"{item or '(no item)'}: {exc}") from None
    stated_key = passage.get("passage_key")
    if stated_key is not None and stated_key != passage_key:
        raise records_mod.RecordProblem(
            f"{item}: the passage states passage_key {str(stated_key)[:12]}, but the page id and "
            "the span's sha256 give another")
    out = {"item": passage.get("item"), "passage_key": passage_key, "span": span,
           "span_sha256": span_sha, "head_words": passage.get("head_words"),
           "quote": dict(cut.quote),
           "section": (list(passage.get("section"))
                       if isinstance(passage.get("section"), (list, tuple))
                       else passage.get("section")),
           "fragment": passage.get("fragment"),
           "passage_kind": passage.get("passage_kind"),
           "prominence": passage.get("prominence"), "scope": passage.get("scope"),
           "statement": passage.get("statement"), "finder": passage.get("finder")}
    for name in vocab.STATEMENT_OPTIONAL:
        if name in passage:
            value = passage[name]
            out[name] = ([dict(row) if isinstance(row, Mapping) else row for row in value]
                         if name == "conjuncts" and isinstance(value, (list, tuple)) else value)
    problems = statements_mod.statement_problems(out, source.get("pageid"),
                                                 item or "(no item)", layer=layer)
    if problems:
        raise records_mod.RecordProblem("; ".join(problems))
    return out


def source_statements(source: Mapping[str, Any], passages: Iterable[Mapping[str, Any]],
                      layer: str) -> tuple[list[dict], list[dict]]:
    """``(the source's sealed statements, the refusals)``, in span then item order.

    The cross-passage rules of the cut are checked here, where every passage of the
    source is in hand: one item and one span text each, no two passages sharing a
    character, and at most one ``subject``.
    """
    kept: list[dict] = []
    refused: list[dict] = []
    for passage in passages:
        try:
            kept.append(statement(source, passage, layer))
        except (records_mod.RecordProblem, keys.KeyProblem) as exc:
            refused.append({"source_id": source.get("source_id"),
                            "item": (passage.get("item") if isinstance(passage, Mapping)
                                     else None), "reason": str(exc)})
    kept.sort(key=lambda row: (row["span"][0], row["item"]))
    out: list[dict] = []
    seen_items: dict[str, dict] = {}
    seen_keys: dict[str, dict] = {}
    subject: dict | None = None
    last: dict | None = None
    for row in kept:
        twin = seen_items.get(row["item"]) or seen_keys.get(row["passage_key"])
        if twin is not None:
            refused.append({"source_id": source.get("source_id"), "item": row["item"],
                            "reason": f"{row['item']} and {twin['item']} are two passages with one "
                                      "item or one span text, which no key could tell apart"})
            continue
        if last is not None and row["span"][0] < last["span"][1]:
            refused.append({"source_id": source.get("source_id"), "item": row["item"],
                            "reason": f"{row['item']} at {row['span']} shares a character with "
                                      f"{last['item']} at {last['span']}"})
            continue
        if row["prominence"] == vocab.PROMINENCE_SUBJECT:
            if subject is not None:
                refused.append({"source_id": source.get("source_id"), "item": row["item"],
                                "reason": f"{row['item']} is a second subject passage beside "
                                          f"{subject['item']}; an article has at most one"})
                continue
            subject = row
        seen_items[row["item"]] = row
        seen_keys[row["passage_key"]] = row
        last = row
        out.append(row)
    return out, refused


# ---- the set ---------------------------------------------------------------------------------


def check_statement_target(out: Path, *, in_checkout: bool = False) -> set[str]:
    """:func:`export_records.check_target` with this format's name, schema and layout."""
    return export_records.check_target(
        out, in_checkout=in_checkout, schemas={vocab.STATEMENT_SET_SCHEMA},
        what="statement set", subdirectories=("statements",))


def _cut_rule(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise StatementExportProblem(f"--{name.replace('_', '-')} is the rule number the cut ran "
                                     f"under: an integer of 1 or more, not {value!r}")
    return value


def build_set(*, inventory: Mapping[str, list], sources: list[dict], layers: Mapping[str, str],
              set_id: str, campaign: str, sourceset: str, sourceset_manifest: str,
              producer_commit: str, created: str,
              span_end_rule: int = DEFAULT_SPAN_END_RULE,
              statement_cut_rule: int = DEFAULT_STATEMENT_CUT_RULE,
              out: Path, in_checkout: bool = False) -> dict:
    """Write the sealed statement set; return the export report.

    ``inventory`` is ``{source_id: [passage]}`` as :func:`cut.frozen_inventory` returns
    it, ``sources`` are the sourceset entries and ``layers`` holds each source's pinned
    text layer, which every span is offsets on and every quote is cut from.
    """
    export_records.check_token(set_id, "--set-id")
    export_records.check_token(sourceset, "--sourceset")
    export_records.check_campaign(campaign)
    export_records.check_created(created)
    export_records.check_commit(producer_commit)
    if not isinstance(sourceset_manifest, str) or not vocab.HEX64.match(sourceset_manifest):
        raise StatementExportProblem("--sourceset-manifest is the manifest's 64-hex sha256")
    cut_rules = {"span_end_rule": _cut_rule(span_end_rule, "span_end_rule"),
                 "statement_cut_rule": _cut_rule(statement_cut_rule, "statement_cut_rule")}
    out = Path(out).expanduser()
    previous = check_statement_target(out, in_checkout=in_checkout)

    by_id: dict[str, dict] = {}
    pages: dict[int, str] = {}
    refused: list[dict] = []
    for entry in sources:
        source_id = str((entry or {}).get("source_id") or "")
        try:
            sources_mod.source_record(entry)             # the family, the licence, the identity
        except sources_mod.SourceProblem as exc:
            refused.append({"source_id": source_id or None, "item": None, "reason": str(exc)})
            continue
        row = {key: entry.get(key) for key in vocab.SOURCE_KEYS}
        why = _source_problem(row, layers.get(source_id))
        if why is None and source_id in by_id:
            why = f"{source_id} is given twice"
        if why is None and pages.setdefault(int(row["pageid"]), source_id) != source_id:
            why = (f"{source_id}: page {row['pageid']} is already "
                   f"{pages[int(row['pageid'])]}; a set holds one revision of a page")
        if why:
            refused.append({"source_id": source_id, "item": None, "reason": why})
            continue
        by_id[source_id] = row

    texts: dict[str, str] = {}
    source_rows: list[dict] = []
    exported = {"sources": 0, "statements": 0}
    for source_id in sorted(inventory):
        source = by_id.get(source_id)
        if source is None:
            if not any(case["source_id"] == source_id for case in refused):
                refused.append({"source_id": source_id, "item": None,
                                "reason": "no row in the sources given"})
            continue
        kept, problems = source_statements(source, inventory[source_id] or [],
                                           layers[source_id])
        refused.extend(problems)
        if not kept:
            continue
        texts[f"statements/{source_id}.jsonl"] = "".join(keys.canonical_line(row) for row in kept)
        source_rows.append(source)
        exported["sources"] += 1
        exported["statements"] += len(kept)
    if not exported["statements"]:
        # Nothing was written, and an empty sealed set is a claim that a cut found
        # nothing — never what happened. The reasons go with the refusal.
        raise StatementExportProblem(
            "no statement to seal: " + ("; ".join(case["reason"] for case in refused[:5])
                                        or "the cut holds no passage"))
    texts["sources.jsonl"] = "".join(keys.canonical_line(row)
                                     for row in sorted(source_rows,
                                                       key=lambda r: str(r["source_id"])))

    export_records.write_files(out, texts, previous)
    seal = {"schema": vocab.STATEMENT_SET_SCHEMA,
            "set_id": set_id,
            "campaign": campaign,
            "sourceset": {"id": sourceset, "manifest_sha256": sourceset_manifest},
            "producer_commit": producer_commit,
            "created": created,
            "cut_rules": cut_rules,
            "files": {rel: keys.sha256_text(text) for rel, text in sorted(texts.items())}}
    seal_text = keys.canonical_line(seal)
    export_records.write_files(out, {"SEAL.json": seal_text})
    return {"schema": EXPORT_REPORT_SCHEMA, "set_id": set_id, "path": str(out),
            "campaign": campaign, "cut_rules": cut_rules, "exported": exported,
            "refused": sorted(refused, key=lambda case: (str(case["source_id"]),
                                                         str(case["item"]))),
            "seal_sha256": keys.sha256_text(seal_text)}


def _source_problem(row: Mapping[str, Any], layer: str | None) -> str | None:
    """Why a source cannot be sealed into a statement set, or None."""
    source_id = row.get("source_id")
    for name in ("html_sha256", "layer_sha256"):
        if not vocab.HEX64.match(str(row.get(name) or "")):
            return f"{source_id}: {name} is not 64 lowercase hex"
    if not vocab.RENDERER_RE.match(str(row.get("renderer") or "")):
        return f"{source_id}: renderer {row.get('renderer')!r} is no renderer id"
    if layer is None:
        return (f"{source_id}: no rendered text layer; a statement's span and quote are offsets "
                "on one layer and it is not here")
    if keys.sha256_text(layer) != row["layer_sha256"]:
        return (f"{source_id}: the rendered layer hashes to {keys.sha256_text(layer)[:12]}… and "
                f"the sourceset pins {str(row['layer_sha256'])[:12]}…; the cut was made against "
                "another layer")
    return None


# ---- the command -------------------------------------------------------------------------------


def from_cut(*, inventory_dir: Path, sourceset_dir: Path, sourceset_id: str | None = None,
             source_ids: Iterable[str] = (), **pins) -> dict:
    """Seal a harvested cut run that lives beside its sourceset."""
    if str(HERE / "gate") not in sys.path:
        sys.path.insert(0, str(HERE / "gate"))
    import cut as cut_mod                        # noqa: PLC0415 — only the command needs these
    import sourceset as sourceset_mod            # noqa: PLC0415

    try:
        ss = sourceset_mod.Sourceset.open(Path(sourceset_dir), sourceset_id)
        manifest = ss.latest()
        frozen = cut_mod.frozen_inventory(Path(inventory_dir))
    except (cut_mod.CutProblem, sourceset_mod.SourcesetError) as exc:
        raise StatementExportProblem(str(exc)) from None
    wanted = sorted(set(source_ids) or set(frozen))
    missing = [sid for sid in wanted if sid not in frozen]
    if missing:
        raise StatementExportProblem(f"the cut holds no document for {', '.join(missing[:5])}")
    layers: dict[str, str] = {}
    entries: list[dict] = []
    for sid in wanted:
        entry = manifest.get(sid)
        if entry is None:
            raise StatementExportProblem(f"the sourceset holds no entry for {sid}")
        entries.append(dict(entry))
        path = ss.dir / "sources" / sid / "layer.txt"
        if path.is_file():
            layers[sid] = path.read_text(encoding="utf-8")
    return build_set(inventory={sid: frozen[sid] for sid in wanted}, sources=entries,
                     layers=layers, sourceset=ss.id,
                     sourceset_manifest=keys.sha256_file(ss.manifest_path(manifest.version)),
                     **pins)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a sealed statement set.")
    parser.add_argument("--inventory", required=True, type=Path,
                        help="the harvested cut run's corpus directory")
    parser.add_argument("--sourceset", required=True, type=Path, help="the sourceset directory")
    parser.add_argument("--sourceset-id")
    parser.add_argument("--out", required=True, type=Path, help="the set's directory")
    parser.add_argument("--set-id", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--producer-commit", required=True,
                        help="the 40-hex commit of what produced the set")
    parser.add_argument("--created", required=True,
                        help="the newest harvest time, so a re-export is byte-identical")
    parser.add_argument("--span-end-rule", type=int, default=DEFAULT_SPAN_END_RULE,
                        help="the span-end rule the cut ran under (default: %(default)s)")
    parser.add_argument("--statement-cut-rule", type=int, default=DEFAULT_STATEMENT_CUT_RULE,
                        help="the statement-cut rule the cut ran under (default: %(default)s)")
    parser.add_argument("--source", action="append", default=[], dest="source_ids")
    parser.add_argument("--report", type=Path, help="write the export report here (default: stdout)")
    args = parser.parse_args(argv)
    try:
        report = from_cut(inventory_dir=args.inventory, sourceset_dir=args.sourceset,
                          sourceset_id=args.sourceset_id, source_ids=args.source_ids,
                          out=args.out, set_id=args.set_id, campaign=args.campaign,
                          producer_commit=args.producer_commit, created=args.created,
                          span_end_rule=args.span_end_rule,
                          statement_cut_rule=args.statement_cut_rule)
    except (StatementExportProblem, export_records.ExportProblem,
            sources_mod.SourceProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"the export failed: {exc!r}", file=sys.stderr)
        return 3
    text = json.dumps(report, indent=1, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
