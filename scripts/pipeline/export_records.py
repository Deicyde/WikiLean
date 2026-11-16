#!/usr/bin/env python3
"""Stage 6: the sealed record set, written.

docs/PIPELINE.md section 3 (stage 6). Outcome rows, sources, a universe and the seal's
pins in; a directory out::

    <set>/SEAL.json                   the pins, and every other file with its sha256
    <set>/universe.json               the universe: its sha, frozen date, libraries by id
    <set>/sources.jsonl               one row per source: ids, revision, title, QID, shas
    <set>/records/<source-id>.jsonl   the Literature Links records
    <set>/receipts/<sha256>.json      the search receipts the absences reference

**The set is a function of its inputs.** The same rows, sources, universe and pins write
byte-identical files, which is what makes a re-export a check rather than a new claim:
canonical JSON throughout, passages by span start then item, locations by rank then
library then declaration, receipts by their own sha256, files sorted in the seal.

**The format is the contract, not the producer's name.** The seal's schema stays
``cf-locate-recordset/1`` or ``/2`` and its keys stay exactly the agreed ones, so a set
this writes and a set formal-companion writes build the same store. ``skill_commit`` is
a 40-hex commit of whatever produced the set and is **given**, never read from a git
HEAD here: WikiLean's producer is a different repository from the one that field was
named for (docs/PIPELINE.md question Q2).

What it refuses, passage by passage, naming each one in the report and exiting non-zero
after writing the rest: a row with no challenge value, no section or no prominence; a
``closeMatch`` with no note; a location whose index row gives no module, path or line; a
coverage claim whose parts are not the passage's ``hasPart`` locations; a record the
vendored Literature Links checker rejects; a quote past the cap. What it refuses whole:
a source whose family may not be quoted, a source over the per-source byte budget when
one is given, and a target directory that is not empty or an earlier set.

**A run planned from a published cut.** ``--statement-set`` names the sealed
``wikilean.statement-set/1`` the locate run was planned from (docs/STATEMENTS.md). Every
passage's quote and ``span_sha256`` are then the **set's**, checked against the pinned
render first (``--rendered-sources``, required with it), and a row whose own cut states
another is refused by name. That is what keeps the statement a reader tagged in phase 1
and the record the pipeline writes in phase 2 one row.

Usage::

    python3 scripts/pipeline/export_records.py --rows <rows.jsonl> --sources <sources.jsonl> \\
        --universe <universe.json> --out <dir> --set-id <token> --campaign <token> \\
        --sourceset <id> --sourceset-manifest <sha256> --producer-commit <40 hex> \\
        --created <ISO-Z> --status-rule <1|2|3> [--byte-budget N] [--report <file>] \\
        [--statement-set <dir> --rendered-sources <dir>]

Exit codes: 0 every row exported, 1 some rows were refused (the set holds the rest),
2 the inputs were refused before anything was written, 3 the writer failed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import keys  # noqa: E402
import links  # noqa: E402
import records as records_mod  # noqa: E402
import sources as sources_mod  # noqa: E402
import vocab  # noqa: E402

EXPORT_REPORT_SCHEMA = "wikilean.pipeline.export/1"
DEFAULT_BYTE_BUDGET = 0


class ExportProblem(ValueError):
    """An input the export refuses before it writes anything."""


# ---- the pins ---------------------------------------------------------------------------


def check_token(value: Any, what: str) -> str:
    if not isinstance(value, str) or not vocab.TOKEN_RE.match(value):
        raise ExportProblem(f"{what} {value!r} is not a token a record set's seal may carry "
                            f"({vocab.TOKEN_RULE})")
    return value


def check_campaign(value: Any) -> str:
    """The seal's campaign: a campaign id, or ``run:<run>[+<challenge run>]``. Never null."""
    if not isinstance(value, str) or not vocab.CAMPAIGN_RE.match(value):
        raise ExportProblem(
            f"campaign {value!r} is not a campaign token: a campaign id (letters, digits and "
            f"._:- , at most 128, never starting {vocab.RUN_TOKEN_PREFIX!r}), or "
            f"run:<run-id>[+<challenge-run-id>]")
    return value


def check_created(value: Any) -> str:
    """The seal's ``created``: the newest harvest time, so a re-export is byte-identical."""
    if not isinstance(value, str) or not vocab.CREATED_RE.match(value):
        raise ExportProblem(f"created {value!r} is not an ISO 8601 UTC time (YYYY-MM-DDThh:mm:ssZ)")
    return value


def check_commit(value: Any) -> str:
    if not isinstance(value, str) or not vocab.HEX40.match(value):
        raise ExportProblem(f"the producer commit {value!r} is not 40 lowercase hex")
    return value


# ---- universe.json ------------------------------------------------------------------------


def written_brain_lib(library: Mapping[str, Any]) -> str:
    """A library's consumer-facing token: its own ``brain_lib``, else its id."""
    return str(library.get("brain_lib") or library.get("id") or "")


def universe_json(universe: Mapping[str, Any]) -> dict:
    """``universe.json``: the universe's sha and frozen date, its libraries as a sorted list.

    Each library carries exactly ``id``, ``brain_lib``, ``repo``, ``commit``,
    ``toolchain``, ``roots``, ``licence`` and ``display_name``. ``display_name`` is
    **derived**, never copied: the library's ``brain_lib`` when that is a name the
    consumer may show, else null — ``brain_lib`` has no display grammar of its own, and
    the consumer refuses the whole set over a name outside its own.
    """
    frozen = str(universe.get("frozen") or "")
    if not vocab.DATE_RE.match(frozen):
        raise ExportProblem(f"universe {universe.get('id')} records no frozen date (YYYY-MM-DD)")
    check_token(universe.get("id"), "the universe id")
    sha = universe.get("universe_sha256")
    if not isinstance(sha, str) or not vocab.HEX64.match(sha):
        raise ExportProblem(f"universe {universe.get('id')} has no 64-hex universe_sha256")
    rows = list(universe.get("libraries") or [])
    if not rows:
        raise ExportProblem(f"universe {universe.get('id')} lists no libraries")
    libraries = []
    for library in sorted(rows, key=lambda lib: str(lib.get("id"))):
        lid = check_token(library.get("id"), "a library id")
        licence = library.get("licence")
        if not (isinstance(licence, dict) and set(licence) == {"spdx"}
                and isinstance(licence["spdx"], str) and licence["spdx"].strip()):
            raise ExportProblem(f"library {lid} records no {{spdx}} licence, and a record set "
                                "names every library's")
        if not vocab.is_repo_url(library.get("repo")):
            raise ExportProblem(f"library {lid} has no repository URL a consumer can link "
                                f"({library.get('repo')!r})")
        if not vocab.HEX40.match(str(library.get("commit") or "")):
            raise ExportProblem(f"library {lid} has no 40-hex commit")
        name = written_brain_lib({**library, "id": lid})
        libraries.append({"id": lid, "brain_lib": name, "repo": library.get("repo"),
                          "commit": library["commit"], "toolchain": library.get("toolchain"),
                          "roots": [str(root) for root in library.get("roots") or []],
                          "licence": {"spdx": licence["spdx"]},
                          "display_name": name if vocab.DISPLAY_NAME_RE.match(name) else None})
    seen_ids = [lib["id"] for lib in libraries]
    if len(set(seen_ids)) != len(seen_ids):
        raise ExportProblem("a universe keys its libraries by id, each id once")
    roots: dict[tuple[str, str], str] = {}
    for library in libraries:
        for root in library["roots"]:
            pair = (library["repo"], root)
            if pair in roots:
                raise ExportProblem(f"root {root} of {library['repo']} is in two libraries "
                                    f"({roots[pair]} and {library['id']}); a repository may be "
                                    "several libraries, but each of its roots belongs to one")
            roots[pair] = library["id"]
    excluded = [{"id": check_token(row.get("id"), "an exclusion id"),
                 "reason": str(row.get("reason") or "")}
                for row in sorted(universe.get("excluded") or [], key=lambda r: str(r.get("id")))]
    for row in excluded:
        if not row["reason"].strip():
            raise ExportProblem(f"exclusion {row['id']} records no reason")
    return {"id": universe["id"], "universe_sha256": sha, "frozen": frozen,
            "libraries": libraries, "excluded": excluded}


# ---- the target ---------------------------------------------------------------------------


#: The one place inside the checkout a set may be written: the synthetic fixture
#: ``scripts/pipeline/fixtures/make_fixture.py`` builds. Only that script passes
#: ``in_checkout``; the command line cannot, so an operator exporting a real set can
#: never put article text in a public repository by mistake.
FIXTURE_ROOT = HERE / "fixtures"


def check_target(out: Path, *, in_checkout: bool = False, schemas: Iterable[str] = (),
                 what: str = "record set", subdirectories: Iterable[str] = ("records", "receipts"),
                 ) -> set[str]:
    """Refuse a target a sealed set may not be written to; return the files its seal names.

    Never this checkout or a directory inside it (``in_checkout`` allows the synthetic
    fixtures alone), never a symlink, and never a directory holding anything but an
    earlier set: a non-empty one needs a ``SEAL.json`` of one of ``schemas`` (the record
    set's by default), and every file in it must be one that seal names. The names come
    back so a re-export can remove what the new set does not carry.

    ``schemas``, ``what`` and ``subdirectories`` are what the statement-set writer
    (:mod:`export_statements`) passes: the rules are the set's, not the format's.
    """
    allowed = set(schemas) or set(vocab.SEAL_SCHEMAS.values())
    out = Path(out).expanduser()
    target = out.resolve()
    inside = target == ROOT or ROOT in target.parents
    fixture = target == FIXTURE_ROOT or FIXTURE_ROOT in target.parents
    if inside and not (in_checkout and fixture):
        raise ExportProblem(f"refusing to write a {what} inside this checkout ({ROOT}): it "
                            "holds code, docs and synthetic fixtures only, and WikiLean is "
                            "public")
    for path in (out, *(out / name for name in subdirectories), out / "SEAL.json"):
        if path.is_symlink():
            raise ExportProblem(f"refusing to write a {what} through the symlink {path}")
    if not out.exists():
        return set()
    if not out.is_dir():
        raise ExportProblem(f"--out {out} is not a directory")
    present = {p.relative_to(out).as_posix() for p in out.rglob("*") if not p.is_dir()}
    if not present:
        return set()
    seal_path = out / "SEAL.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8")) if seal_path.is_file() else None
    if not isinstance(seal, dict) or seal.get("schema") not in allowed:
        raise ExportProblem(f"{out} is not empty and holds no {what} seal; pass an empty or "
                            "new directory with --out")
    named = set(seal.get("files") or {})
    stray = sorted(present - named - {"SEAL.json"})
    if stray:
        raise ExportProblem(f"{out} holds file(s) its seal does not name: {', '.join(stray[:5])}; "
                            f"a {what} directory holds the set and nothing else")
    return named


def write_files(out: Path, texts: Mapping[str, str], previous: Iterable[str] = ()) -> None:
    """Write the set's files, then remove what the previous seal named and this set does not."""
    out.mkdir(parents=True, exist_ok=True)
    for rel in sorted(set(previous) - set(texts)):
        path = out / rel
        if path.is_file() and not path.is_symlink():
            path.unlink()
    for rel, text in sorted(texts.items()):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


# ---- the set ------------------------------------------------------------------------------


def row_cut(row: Mapping[str, Any],
            sealed: Mapping[tuple[str, str], records_mod.Cut] | None = None,
            spans: Mapping[tuple[str, str], list[int]] | None = None,
            ) -> records_mod.Cut:
    """The passage's cut: the row's own, or a sealed statement set's.

    Without ``sealed`` this is what the text layer gave the passage, as the tally derived
    it (docs/PIPELINE.md stage 5). With it — the cuts of a ``wikilean.statement-set/1``,
    checked against the pinned render first — the **set's** cut is used, and a row whose
    own cut states another quote or another ``span_sha256`` is refused by name: the
    published statement and the record of it are one passage or they are nothing.

    ``spans`` is the same set's spans (``StatementSet.spans``). A record carries its
    row's ``span`` beside the cut's ``span_sha256`` (``records.subject_end``), so the
    row's span is held to the sealed one too: a row whose cut is absent, or whose cut
    agrees while its span does not, would otherwise publish the sealed hash of one
    stretch of text over the offsets of another.
    """
    cut = row.get("cut")
    if not isinstance(cut, Mapping):
        if sealed is None:
            raise records_mod.RecordProblem(
                f"{row.get('item')}: the row carries no cut {{quote, span_sha256}}; a record "
                "cannot be written without the quote and the span's sha256 the layer gave it")
        cut = {}
    own = records_mod.Cut(dict(cut.get("quote") or {}), str(cut.get("span_sha256") or ""))
    if sealed is None:
        return own
    key = (str(row.get("source_id")), str(row.get("item")))
    published = sealed.get(key)
    if published is None:
        raise records_mod.RecordProblem(
            f"{row.get('item')}: the statement set holds no statement of {key[0]} with this "
            "item, and the set is what says which passages were cut")
    if cut and (own.quote != published.quote or own.span_sha256 != published.span_sha256):
        raise records_mod.RecordProblem(
            f"{row.get('item')}: the row's cut is not the published one — the set sealed "
            f"span_sha256 {published.span_sha256[:12]} and quote "
            f"{published.quote.get('exact', '')[:40]!r}")
    want = (spans or {}).get(key)
    if want is not None and list(row.get("span") or []) != want:
        raise records_mod.RecordProblem(
            f"{row.get('item')}: the row's span {row.get('span')!r} is not the published "
            f"{want!r}, and the record carries its row's span beside the set's span_sha256")
    return published


def build_set(*, rows: list[dict], sources: list[dict], universe: Mapping[str, Any],
              set_id: str, campaign: str, sourceset: str, sourceset_manifest: str,
              producer_commit: str, created: str, status_rule: int,
              byte_budget: int = DEFAULT_BYTE_BUDGET, out: Path,
              layers: Mapping[str, str] | None = None,
              statement_cuts: Mapping[tuple[str, str], records_mod.Cut] | None = None,
              statement_spans: Mapping[tuple[str, str], list[int]] | None = None,
              in_checkout: bool = False) -> dict:
    """Write the sealed set; return the export report.

    ``statement_cuts`` is a published cut's, keyed ``(source_id, item)``
    (``statements.StatementSet.cuts``): with it every passage's quote and ``span_sha256``
    are the statement set's, and a row that states another is refused (:func:`row_cut`).
    ``statement_spans`` is that set's spans (``StatementSet.spans``), which the row's own
    span is held to for the same reason.
    """
    problem = vocab.status_rule_problem(status_rule)
    if problem:
        raise ExportProblem(problem)
    check_token(set_id, "--set-id")
    check_token(sourceset, "--sourceset")
    check_campaign(campaign)
    check_created(created)
    check_commit(producer_commit)
    if not isinstance(sourceset_manifest, str) or not vocab.HEX64.match(sourceset_manifest):
        raise ExportProblem("--sourceset-manifest is the manifest's 64-hex sha256")
    if type(byte_budget) is not int or byte_budget < 0:
        raise ExportProblem("--byte-budget is an integer, 0 or more (0: no budget)")
    universe_doc = universe_json(universe)
    universe_view = {"id": universe_doc["id"], "sha256": universe_doc["universe_sha256"],
                     "libraries": universe_doc["libraries"]}
    out = Path(out).expanduser()
    previous = check_target(out, in_checkout=in_checkout)
    layers = dict(layers or {})

    by_id: dict[str, dict] = {}
    refused: list[dict] = []
    for entry in sources:
        try:
            record = sources_mod.source_record(entry)
        except sources_mod.SourceProblem as exc:
            refused.append({"source_id": entry.get("source_id"), "item": None, "reason": str(exc)})
            continue
        by_id[record["source_id"]] = record

    by_source: dict[str, list[dict]] = {}
    for row in rows:
        source_id = str(row.get("source_id"))
        if source_id not in by_id:
            refused.append({"source_id": source_id, "item": row.get("item"),
                            "reason": "no row in the sources given"})
            continue
        by_source.setdefault(source_id, []).append(row)

    texts: dict[str, str] = {}
    sidecars: dict[str, str] = {}
    source_rows: list[dict] = []
    exported: Counter = Counter()
    for source_id in sorted(by_source):
        source = by_id[source_id]
        layer = layers.get(source_id, "")
        lines: list[str] = []
        own: dict[str, str] = {}
        kept = 0
        for row in sorted(by_source[source_id],
                          key=lambda r: ((r.get("span") or [0])[0], str(r.get("item")))):
            try:
                made, cars = records_mod.passage_records(source, row,
                                                         row_cut(row, statement_cuts,
                                                                 statement_spans),
                                                         universe_view,
                                                         status_rule=status_rule, layer=layer)
            except (records_mod.RecordProblem, keys.KeyProblem) as exc:
                refused.append({"source_id": source_id, "item": row.get("item"),
                                "reason": str(exc)})
                continue
            problems = links.validate_all(made)
            if problems:
                refused.append({"source_id": source_id, "item": row.get("item"),
                                "reason": "; ".join(problems[:3])})
                continue
            lines.extend(keys.canonical(record) for record in made)
            own.update(cars)
            kept += 1
            exported["records"] += len(made)
        if not lines:
            continue
        text = "\n".join(lines) + "\n"
        size = len(text.encode("utf-8")) + sum(len(car.encode("utf-8")) for car in own.values())
        if byte_budget and size > byte_budget:
            refused.append({"source_id": source_id, "item": None,
                            "reason": f"records/{source_id}.jsonl and its receipts would be "
                                      f"{size} bytes, over the per-source budget of {byte_budget}"})
            exported["records"] -= len(lines)
            continue
        texts[f"records/{source_id}.jsonl"] = text
        sidecars.update(own)
        source_rows.append({key: source[key] for key in vocab.SOURCE_KEYS})
        exported["sources"] += 1
        exported["passages"] += kept
    for digest, car in sorted(sidecars.items()):
        texts[f"receipts/{digest}.json"] = car
    texts["universe.json"] = keys.canonical_line(universe_doc)
    texts["sources.jsonl"] = "".join(keys.canonical_line(row) for row in source_rows)

    write_files(out, texts, previous)
    seal = {"schema": vocab.SEAL_SCHEMAS[status_rule],
            "set_id": set_id,
            "campaign": campaign,
            "universe": {"id": universe_doc["id"],
                         "universe_sha256": universe_doc["universe_sha256"]},
            "sourceset": {"id": sourceset, "manifest_sha256": sourceset_manifest},
            "skill_commit": producer_commit,
            "created": created,
            "byte_budget": int(byte_budget),
            "files": {rel: keys.sha256_text(text) for rel, text in sorted(texts.items())}}
    if status_rule >= vocab.STATUS_RULE_BUNDLE:
        seal["status_rule"] = status_rule
    seal_text = keys.canonical_line(seal)
    write_files(out, {"SEAL.json": seal_text})
    pin = links.pin()
    return {"schema": EXPORT_REPORT_SCHEMA, "set_id": set_id, "path": str(out),
            "campaign": campaign, "status_rule": status_rule, "byte_budget": int(byte_budget),
            "exported": dict(sorted(exported.items())), "refused": refused,
            "seal_sha256": keys.sha256_text(seal_text),
            "links_version": pin["version"], "links_schema_sha256": pin["sha256"]}


# ---- the command ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ExportProblem(f"{path} line {number}: not an object")
            out.append(value)
    return out


def _published_cut(statement_set: Path, rendered_sources: Path, sources: list[dict]
                   ) -> tuple[dict, dict, dict[str, str]]:
    """``(the set's cuts and spans by (source, item), each source's layer)``, checked twice over.

    Against the **render**: every layer hashes to the ``layer_sha256`` the set was cut
    from, and every span and quote is the sealed one
    (``statements.frozen_inventory``). And against the **rows being sealed**: the
    sources.jsonl this export will write must pin the same revision and the same bytes
    the cut was made against (``statements.manifest_problems``) — a record set whose
    sources name another layer would publish spans that are offsets on nothing.

    The layers come back too, so the export holds every free-text field it writes to the
    quote cap (``records.check_free_text``), which a bare row export cannot.
    """
    import statements as statements_mod  # noqa: PLC0415 — only a published cut needs it

    layers: dict[str, str] = {}
    for path in sorted(Path(rendered_sources).glob("*/layer.txt")):
        layers[path.parent.name] = path.read_text(encoding="utf-8")
    try:
        sealed = statements_mod.read_set(Path(statement_set))
        found = statements_mod.manifest_problems(
            sealed, {str(row.get("source_id")): row for row in sources})
        if found:
            raise ExportProblem("\n".join(found))
        statements_mod.frozen_inventory(sealed, Path(rendered_sources))
        return sealed.cuts(), sealed.spans(), layers
    except statements_mod.StatementSetProblem as exc:
        raise ExportProblem(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a sealed locate record set.")
    parser.add_argument("--rows", required=True, type=Path, help="the outcome rows, as JSONL")
    parser.add_argument("--sources", required=True, type=Path, help="the sourceset entries, as JSONL")
    parser.add_argument("--universe", required=True, type=Path, help="the universe, as JSON")
    parser.add_argument("--out", required=True, type=Path, help="the set's directory")
    parser.add_argument("--set-id", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--sourceset", required=True)
    parser.add_argument("--sourceset-manifest", required=True)
    parser.add_argument("--producer-commit", required=True,
                        help="the 40-hex commit of what produced the set")
    parser.add_argument("--created", required=True,
                        help="the newest harvest time, so a re-export is byte-identical")
    parser.add_argument("--status-rule", required=True, type=int, choices=vocab.STATUS_RULES)
    parser.add_argument("--statement-set", type=Path,
                        help="the sealed wikilean.statement-set/1 the run was planned from "
                             "(docs/STATEMENTS.md): every passage's quote and span_sha256 are "
                             "the set's, and a row that states another is refused. Needs "
                             "--rendered-sources")
    parser.add_argument("--rendered-sources", type=Path,
                        help="the sourceset's rendered sources (<dir>/<source-id>/layer.txt), "
                             "which goes with --statement-set and only with it: every "
                             "span_sha256 is recomputed from the pinned render and a difference "
                             "refuses the export, and the layers hold every free-text field to "
                             "the quote cap")
    parser.add_argument("--byte-budget", type=int, default=DEFAULT_BYTE_BUDGET)
    parser.add_argument("--report", type=Path, help="write the export report here (default: stdout)")
    args = parser.parse_args(argv)
    if (args.statement_set is None) != (args.rendered_sources is None):
        parser.error("--statement-set and --rendered-sources go together: the published cut is "
                     "checked against the pinned render before a record is written from it, and "
                     "a render nothing is checked against would only weaken the quote cap")
    try:
        source_rows = _read_jsonl(args.sources)
        cuts, spans, layers = ((None, None, {}) if args.statement_set is None else
                               _published_cut(args.statement_set, args.rendered_sources, source_rows))
        report = build_set(rows=_read_jsonl(args.rows), sources=source_rows,
                           universe=json.loads(args.universe.read_text(encoding="utf-8")),
                           set_id=args.set_id, campaign=args.campaign, sourceset=args.sourceset,
                           sourceset_manifest=args.sourceset_manifest,
                           producer_commit=args.producer_commit, created=args.created,
                           status_rule=args.status_rule, byte_budget=args.byte_budget,
                           layers=layers, statement_cuts=cuts, statement_spans=spans,
                           out=args.out)
    except (ExportProblem, links.LinksProblem, sources_mod.SourceProblem) as exc:
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
