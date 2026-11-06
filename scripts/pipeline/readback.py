#!/usr/bin/env python3
"""Stage 4: a sealed record set read back into the rows the tally produced.

docs/PIPELINE.md section 3 (stage 4). The inverse of stage 5: a sealed set in, and the
seal, the universe, the sources and one **outcome row** per passage out.

It is not only a test harness. It is what a promotion into ``brain/proposals/*.jsonl``
reads (docs/INTAKE.md section 6, step 1), what lets an operator diff two sets of one
page, and what makes a re-export possible without the producer's workspace. It is also
the honest boundary of the batch-1 proof: the capped quote and the span's sha256 come
from the article's text layer, which is in no sealed set and in no checkout here, so
they are carried through as the :class:`records.Cut` the record already states, while
every other value is re-derived (docs/PIPELINE.md section 8).

The outcome row (``wikilean.pipeline.outcome/1``)::

    {"source_id", "item", "passage_kind", "span": [start, end], "section": [...],
     "prominence", "challenge", "status" | null, "outcome": located|absent|unresolved,
     "locations": [{"location_key", "relation", "library", "declaration", "module",
                    "path", "line", "end_line"?, "proof_status", "statement"?,
                    "rank", "confidence", "challenge", "note"?}],
     "cut": {"quote": {"exact", "prefix", "suffix"}, "span_sha256"},
     "absence": {"challenge", "receipt": {"queries": [...], "candidates": [...]}}?,
     "coverage": {...}?}

``cut`` is what the text layer gave the passage. Every other field is re-derived from
the records; the cut is carried, because the layer is in no sealed set.

Usage::

    python3 scripts/pipeline/readback.py --record-set <dir> [--out <rows.jsonl>]
    python3 scripts/pipeline/readback.py --record-set <dir> --verify-only

Exit codes: 0 read, 2 the set was refused (the reasons on stderr), 3 the reader failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import keys  # noqa: E402
import records as records_mod  # noqa: E402
import sources as sources_mod  # noqa: E402
import vocab  # noqa: E402

ROW_SCHEMA = "wikilean.pipeline.outcome/1"


class SetProblem(ValueError):
    """A sealed record set this pipeline refuses to read, with every reason it found."""


# ---- the seal -------------------------------------------------------------------------


def verify_set(set_dir: Path) -> list[str]:
    """What a consumer checks of a sealed set: every file the seal names, and nothing else.

    Every file the seal lists is present with its sha256, no file it does not list is
    there, and nothing is a symlink or escapes the directory. Returns the problems;
    empty means the seal holds.
    """
    set_dir = Path(set_dir)
    problems: list[str] = []
    seal_path = set_dir / "SEAL.json"
    if not seal_path.is_file():
        return ["SEAL.json: missing"]
    try:
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"SEAL.json: not JSON ({exc})"]
    if not isinstance(seal, dict) or not isinstance(seal.get("files"), dict):
        return ["SEAL.json: no files map"]
    named = dict(seal["files"])
    for rel, sha in sorted(named.items()):
        path = set_dir / rel
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            problems.append(f"{rel}: the seal names a path outside the set")
        elif path.is_symlink():
            problems.append(f"{rel}: a symlink")
        elif not path.is_file():
            problems.append(f"{rel}: missing")
        elif keys.sha256_file(path) != sha:
            problems.append(f"{rel}: sha256 does not match the seal")
    for path in sorted(set_dir.rglob("*")):
        rel = path.relative_to(set_dir).as_posix()
        if path.is_symlink():
            problems.append(f"{rel}: a symlink")
        elif path.is_file() and rel != "SEAL.json" and rel not in named:
            problems.append(f"{rel}: not in the seal")
    return problems


def seal_status_rule(seal: dict) -> int:
    """The status rule a seal's records were derived under: 1 for /1, its own for /2."""
    schema = seal.get("schema")
    if schema == vocab.SEAL_SCHEMA:
        if "status_rule" in seal:
            raise SetProblem(f"a {vocab.SEAL_SCHEMA} seal carries no status_rule")
        return vocab.STATUS_RULE_D1
    if schema != vocab.SEAL_SCHEMA_BUNDLE:
        raise SetProblem(f"the seal's schema {schema!r} is not a record set this pipeline reads")
    rule = seal.get("status_rule")
    if rule not in vocab.BUNDLE_STATUS_RULES or type(rule) is not int:
        raise SetProblem(f"a {vocab.SEAL_SCHEMA_BUNDLE} seal's status_rule is "
                         f"{list(vocab.BUNDLE_STATUS_RULES)}, not {rule!r}")
    return rule


# ---- the set --------------------------------------------------------------------------


@dataclass
class SealedSet:
    """A sealed record set, read back."""

    path: Path
    seal: dict
    seal_sha256: str
    status_rule: int
    universe: dict                     # universe.json, as sealed
    universe_view: dict                # {id, sha256, libraries} — what records.py takes
    sources: list[dict]                # sources.jsonl rows, with uri/container_title/licence
    rows: list[dict]                   # one outcome row per passage, in record-set order
    cuts: dict[tuple[str, str], records_mod.Cut] = field(default_factory=dict)

    def source(self, source_id: str) -> dict:
        for row in self.sources:
            if row["source_id"] == source_id:
                return row
        raise SetProblem(f"{source_id}: no such source in the set")

    def cut(self, row: dict) -> records_mod.Cut:
        return self.cuts[(row["source_id"], row["item"])]


def _lines(text: str, where: str) -> list[dict]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SetProblem(f"{where} line {number}: not JSON ({exc})") from exc
        if not isinstance(value, dict):
            raise SetProblem(f"{where} line {number}: not an object")
        out.append(value)
    return out


def _ext(node: Any, where: str) -> dict:
    extensions = node.get("extensions") if isinstance(node, dict) else None
    if not isinstance(extensions, dict) or not isinstance(extensions.get(vocab.LOCATE), dict):
        raise SetProblem(f"{where}: no locate extension")
    return extensions[vocab.LOCATE]


def _agree(rows: list[dict], key: str, where: str) -> Any:
    """One value every record of a passage must carry; refuses a disagreement."""
    seen = [row.get(key) for row in rows]
    first = seen[0]
    for value in seen[1:]:
        if value != first:
            raise SetProblem(f"{where}: the records of one passage disagree on {key}")
    return first


def _location(record: dict, ext: dict, where: str) -> dict:
    objects = record.get("object")
    if not isinstance(objects, list) or len(objects) != 1:
        raise SetProblem(f"{where}: a location record has exactly one lean end")
    end = objects[0]
    lean = _ext(end, f"{where} (its lean end)")
    place = end.get("location") if isinstance(end.get("location"), dict) else {}
    out = {"location_key": ext.get("location_key"), "relation": record.get("relation"),
           "library": lean.get("library"), "declaration": place.get("declaration"),
           "module": place.get("module"), "path": lean.get("path"), "line": lean.get("line"),
           "proof_status": lean.get("proof_status"), "rank": ext.get("rank"),
           "confidence": ext.get("confidence"), "challenge": ext.get("challenge")}
    if "end_line" in lean:
        out["end_line"] = lean["end_line"]
    if lean.get("statement"):
        out["statement"] = True
    if record.get("note"):
        out["note"] = record["note"]
    return out


def _passage_row(source_id: str, group: list[dict], status_rule: int
                 ) -> tuple[dict, records_mod.Cut]:
    """One passage's records as an outcome row and the cut they state."""
    where = str(group[0].get("id"))
    exts = [_ext(record, where) for record in group]
    subject_ends = []
    for record in group:
        subject = record.get("subject")
        if not isinstance(subject, list) or len(subject) != 1:
            raise SetProblem(f"{where}: a record has exactly one subject end")
        subject_ends.append(subject[0])
    subject_exts = [_ext(end, f"{where} (its subject end)") for end in subject_ends]

    span = _agree(subject_exts, "span", where)
    if span is None:
        raise SetProblem(f"{where}: the subject end carries no span, so the passage cannot be "
                         "placed on its layer and the set cannot be re-exported (a set from "
                         "before the producer's split-frontend commit)")
    span_sha = _agree(subject_exts, "span_sha256", where)
    quote = _agree([end.get("location") or {} for end in subject_ends], "quote", where)
    if not isinstance(quote, dict):
        raise SetProblem(f"{where}: the subject end carries no quote")
    cut = records_mod.Cut({"exact": quote.get("exact"), "prefix": quote.get("prefix", ""),
                           "suffix": quote.get("suffix", "")}, str(span_sha))

    row: dict = {"source_id": source_id, "item": _agree(exts, "item", where),
                 "passage_kind": _agree(group, "kind", where),
                 "span": list(span),
                 "cut": {"quote": dict(cut.quote), "span_sha256": cut.span_sha256},
                 "section": _agree(exts, "section", where),
                 "prominence": _agree(exts, "prominence", where)}
    coverage = _agree(exts, "coverage", where)
    if coverage is not None:
        row["coverage"] = coverage
    if status_rule >= vocab.STATUS_RULE_BUNDLE:
        row["passage_key"] = _agree(exts, "passage_key", where)

    locations: list[dict] = []
    absence: dict | None = None
    unresolved: dict | None = None
    status: Any = None
    for record, ext in zip(group, exts):
        if record.get("relation") == vocab.NO_MATCH:
            absence = {"challenge": ext.get("challenge"), "receipt": ext.get("receipt")}
        elif not record.get("object"):
            unresolved = {"challenge": ext.get("challenge")}
        else:
            locations.append(_location(record, ext, where))
            status = record.get("status")
    row["locations"] = locations
    if absence is not None:
        row["outcome"] = "absent"
        row["absence"] = absence
        row["status"] = vocab.NOT_FORMALIZED
        row["challenge"] = absence["challenge"]
    elif unresolved is not None:
        row["outcome"] = "unresolved"
        row["status"] = None
        row["challenge"] = unresolved["challenge"]
    else:
        if not locations:
            raise SetProblem(f"{where}: a passage with no record of any shape")
        row["outcome"] = "located"
        row["status"] = status
        # A location record carries its own challenge, so the passage's is not recoverable
        # and is not needed: every record of this passage overwrites it. The rank-1
        # location's keeps a re-export byte-identical.
        row["challenge"] = locations[0]["challenge"]
    return row, cut


def _receipts(set_dir: Path, files: dict[str, bytes], rows: list[dict]) -> None:
    """Replace each absence's receipt reference with the sidecar's own queries and candidates."""
    for row in rows:
        absence = row.get("absence")
        if not absence:
            continue
        reference = absence.get("receipt")
        if not isinstance(reference, dict) or not vocab.HEX64.match(str(reference.get("sha256", ""))):
            raise SetProblem(f"{row['source_id']}::{row['item']}: the absence's receipt is not "
                             "{sha256, queries, candidates}")
        rel = f"receipts/{reference['sha256']}.json"
        if rel not in files:
            raise SetProblem(f"{row['source_id']}::{row['item']}: {rel} is not in the set")
        body = json.loads(files[rel].decode("utf-8"))
        for name in ("queries", "candidates"):
            if reference.get(name) != len(body.get(name) or []):
                raise SetProblem(f"{row['source_id']}::{row['item']}: receipt.{name} is "
                                 f"{reference.get(name)!r}, but {rel} lists "
                                 f"{len(body.get(name) or [])}")
        absence["receipt"] = {"queries": body.get("queries") or [],
                              "candidates": body.get("candidates") or []}


def read_set(set_dir: Path) -> SealedSet:
    """Read a sealed record set: check its seal, then read it back into outcome rows."""
    set_dir = Path(set_dir)
    problems = verify_set(set_dir)
    if problems:
        raise SetProblem("\n".join(problems))
    seal_bytes = (set_dir / "SEAL.json").read_bytes()
    seal = json.loads(seal_bytes.decode("utf-8"))
    status_rule = seal_status_rule(seal)
    files = {rel: (set_dir / rel).read_bytes() for rel in seal["files"]}

    universe = json.loads(files["universe.json"].decode("utf-8"))
    if (universe.get("id") != (seal.get("universe") or {}).get("id")
            or universe.get("universe_sha256") != (seal.get("universe") or {}).get("universe_sha256")):
        raise SetProblem("universe.json does not name the universe the seal pins")
    universe_view = {"id": universe["id"], "sha256": universe["universe_sha256"],
                     "libraries": list(universe["libraries"])}

    source_rows = [sources_mod.source_record(entry)
                   for entry in _lines(files["sources.jsonl"].decode("utf-8"), "sources.jsonl")]
    by_id = {row["source_id"]: row for row in source_rows}
    if len(by_id) != len(source_rows):
        raise SetProblem("sources.jsonl names one source twice")

    rows: list[dict] = []
    cuts: dict[tuple[str, str], records_mod.Cut] = {}
    for rel in sorted(seal["files"]):
        if not rel.startswith("records/"):
            continue
        source_id = rel[len("records/"):-len(".jsonl")]
        if source_id not in by_id:
            raise SetProblem(f"{rel}: no row in sources.jsonl")
        groups: dict[str, list[dict]] = {}
        for record in _lines(files[rel].decode("utf-8"), rel):
            groups.setdefault(str(record.get("id")), []).append(record)
        for group in groups.values():
            row, cut = _passage_row(source_id, group, status_rule)
            rows.append(row)
            cuts[(source_id, row["item"])] = cut
    missing = sorted(set(by_id) - {row["source_id"] for row in rows})
    if missing:
        raise SetProblem(f"sources.jsonl names source(s) with no records file: {', '.join(missing)}")
    _receipts(set_dir, files, rows)

    return SealedSet(path=set_dir, seal=seal, seal_sha256=keys.sha256_bytes(seal_bytes),
                     status_rule=status_rule, universe=universe, universe_view=universe_view,
                     sources=source_rows, rows=rows, cuts=cuts)


# ---- the command ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read a sealed locate record set back into its outcome rows.")
    parser.add_argument("--record-set", required=True, type=Path,
                        help="the sealed set's directory")
    parser.add_argument("--out", type=Path, help="write the rows as JSONL here (default: stdout)")
    parser.add_argument("--verify-only", action="store_true",
                        help="check the seal and report, reading no records")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            problems = verify_set(args.record_set)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 2
            print(f"{args.record_set}: the seal holds")
            return 0
        read = read_set(args.record_set)
    except SetProblem as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"the reader failed: {exc!r}", file=sys.stderr)
        return 3
    text = "".join(keys.canonical_line({"schema": ROW_SCHEMA, **row}) for row in read.rows)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"{len(read.rows)} rows from {len(read.sources)} sources -> {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
