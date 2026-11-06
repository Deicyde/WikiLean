#!/usr/bin/env python3
"""Turn an accepted contributed record set into reviewable overlay rows. Offline, never D1.

The public-editing plan, section 3.3. ``verify_submission.py`` says whether a set may be
accepted; this says what it would become. It is the coordinator's command, run on the
coordinator's machine, after the verifier has passed a set:

    verify_submission.py  ──►  promote_submission.py  ──►  article_tags rows
      (the intake contract)      (this: offline, a file)     (R1's overlay table)

**It never writes to D1.** No binding, no wrangler, no network, no token: it writes a
``.sql`` file, or a ``.json`` file, or both, and a person reads them before anything is
applied. That is the whole point of it being a separate command — an accepted set is
still untrusted input, and the step between "the bytes check out" and "this is on the
site" should be a diff somebody looked at.

**One row per `pair_key`, which is the overlay's own key.** ``pair_key =
sha256(passage_key \\n repository \\n declaration)`` is revision-free and commit-free, so
a contributed location collides *by construction* with the pipeline's own location on
the same (passage, repository, declaration). Two sets about one passage therefore
produce **one row and a second attribution**, never two rows that can disagree. Where
two sets name one pair the later one on the command line wins and the report says so;
where one set names a pair twice with two different judgements the pair is **refused**,
because a reviewable file should hold no row nobody can defend.

**Supersession is in the SQL, not in a convention.** R1 stores all mutable rows as
``origin='community'``. An import uses ``via='intake'`` and may supersede only a live
agent row from a previous intake. Human rows, web/API edits and tombstones are
**never overwritten**. Pipeline locations live in the sealed store, not this table.
That is ``findLostHuman``'s principle restated for this plane, and putting it in the generated statement means it
holds even if the person applying the file has not read section 3.3.

**What is not promoted, and why.** A checked absence (``noMatch``) carries no pair —
there is no declaration to key a row on — so it has no place in a table whose primary
key is ``pair_key``. Absences are counted in the report and named, never dropped
silently. A coverage claim is likewise not a row: its *parts* are, and they are already
locations.

**What this command cannot infer stays NULL**: ``base_relation`` needs the published
store, and ``removed_*`` belongs to a removal, which this command never makes.
The accepted set supplies the article revision; new rows start their own version at 1.

Usage (CPython 3.12, as build_store needs):

    python3 scripts/articles/promote_submission.py --record-set <dir> \\
        --added-by <user-id> --created-at 2026-11-06T00:00:00Z \\
        [--machine-id <id>] [--token-id <id>] [--model <name>] [--run-id <id>] \\
        [--trusted] [--actor-type agent|human] [--via <token>] \\
        [--sql <file>] [--json <file>] [--report <file>]

Exit codes: 0 the rows are written, 1 they are written and something in the set was not
promotable (the list is in the report), 2 the set was refused by the intake contract or
by this command (the reason on stderr), 3 the promoter itself failed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_store  # noqa: E402
import verify_submission as intake  # noqa: E402

PROMOTION_SCHEMA = "wikilean.articles.promotion/1"
PROMOTER = "scripts/articles/promote_submission.py/1"

#: R1's overlay table (the plan's section 2.9, migration 0019). The column order here is
#: the order the generated INSERT writes. Tests apply its SQL to the actual shipped
#: migration, rather than deriving a permissive fake table from this list.
TABLE = "article_tags"
COLUMNS = ("pair_key", "pageid", "passage_key", "library", "repository", "declaration",
           "relation", "note", "state", "origin", "base_relation", "added_by",
           "actor_type", "via", "token_id", "model", "run_id", "campaign", "revision", "record_set",
           "universe_sha256", "version", "created_at", "updated_at", "removed_by",
           "removed_at", "removed_reason")
#: R1's origin vocabulary; via='intake' distinguishes an imported submission.
ORIGIN = "community"
#: ``state`` is ``live`` for a trusted submitter and ``pending`` for everybody else; the
#: promoter is told which, it does not decide it. R1 has no pending table: pending
#: rows are review JSON only, and SQL requires explicit --trusted approval.
STATE_LIVE, STATE_PENDING = "live", "pending"
#: The actor vocabulary decision 7 recommends unifying on.
ACTOR_TYPES = ("agent", "human")
#: Origin alone does not grant supersession: R1 uses community for every row.
#: The generated WHERE also requires intake, agent, live, and no tombstone.
SUPERSEDABLE = ("community",)
INTAKE_VIA = "intake"
#: The columns a supersession writes. Three are left out on purpose:
#:
#: * ``pair_key`` is the conflict target and ``created_at`` says when the row first
#:   existed, which a supersession does not change;
#: * ``version`` is incremented rather than overwritten;
#: * the three ``removed_*`` columns are **not here**, and nor is the update allowed to
#:   run against a removed row at all (``AND removed_at IS NULL``). Writing them would
#:   set a moderator's tombstone back to NULL and bring a removed row live again, which
#:   is the same defect as overwriting a human's edit wearing a different hat.
UPDATED = tuple(name for name in COLUMNS
                if name not in ("pair_key", "created_at", "version",
                                "removed_by", "removed_at", "removed_reason"))
#: The version a newly inserted row starts at. An existing row's is incremented by the
#: generated statement.
FIRST_VERSION = 1
#: The free-text cap the tag form and the feedback form share (the plan's section 2.2).
MAX_NOTE = 500
#: A campaign token names its run as ``run:<run>[+<challenge run>]`` (section 3.6).
_CAMPAIGN_RUN = re.compile(r"^run:([A-Za-z0-9][A-Za-z0-9._-]*)")
#: Anything a SQL literal may not carry. A NUL or a control character in a value is a
#: defect of the set, not something to escape around.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: The columns whose value is a **structural** name — a library, a repository, a Lean
#: declaration, a relation, a key. None of them may carry a line break, and a set that
#: puts one there is refused rather than escaped around: the line break is the thing
#: that turns a comment into a statement (:func:`sql_for`).
LINE_SAFE = ("pair_key", "passage_key", "library", "repository", "declaration",
             "relation", "state", "origin", "base_relation", "added_by", "actor_type",
             "via", "token_id", "model", "run_id", "campaign", "record_set",
             "universe_sha256", "created_at", "updated_at", "removed_by", "removed_at")
_LINE_BREAK = re.compile("[\r\n\u2028\u2029\u0085]")


class PromotionProblem(ValueError):
    """A set this command will not promote, named rather than promoted anyway."""


# ---- the rows ---------------------------------------------------------------------------


def timestamp_ms(value: str) -> int:
    """The Worker's timestamp unit, derived from the supplied clock without a float."""
    try:
        if not build_store.CREATED_RE.match(value):
            raise ValueError("expected an ISO UTC timestamp ending in Z")
        at = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except (TypeError, ValueError) as exc:
        raise PromotionProblem("--created-at is a valid ISO 8601 UTC time ending in Z") from exc
    delta = at - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def run_of(campaign: str) -> str | None:
    """The locate run a campaign token names, or None when it names none."""
    found = _CAMPAIGN_RUN.match(str(campaign or ""))
    return found.group(1) if found else None


def rows_of_article(view: dict, *, state: str, added_by: str, actor_type: str, via: str,
                    token_id: str | None, model: str | None, run_id: str | None,
                    created_at: str) -> tuple[list[dict], list[str]]:
    """``(rows, notes)`` for one article's view model, one row per located pair."""
    rows: list[dict] = []
    notes: list[str] = []
    pageid = view["pageid"]
    at_ms = timestamp_ms(created_at)
    campaign = view["campaign"]
    record_set = view["record_set"]
    universe_sha = view["universe"]["sha256"]
    run = run_id if run_id is not None else run_of(campaign)
    for passage in view["passages"]:
        if passage.get("absence") is not None:
            notes.append(
                f"{view['source_id']} {passage['item']}: a checked absence, which has no "
                "declaration and therefore no pair_key; it is not an overlay row")
        if passage.get("unresolved") is not None:
            notes.append(f"{view['source_id']} {passage['item']}: unresolved, no row")
        for location in passage.get("locations") or []:
            pair = location.get("pair_key")
            if not pair:
                notes.append(
                    f"{view['source_id']} {passage['item']} {location['declaration']}: "
                    "no pair_key, so no overlay row (a /1 set carries none, and intake "
                    "refuses /1)")
                continue
            note = location.get("note")
            if isinstance(note, str) and len(note) > MAX_NOTE:
                notes.append(f"{pair[:12]}: the note is {len(note)} characters and the "
                             f"overlay caps it at {MAX_NOTE}; it is truncated")
                note = note[:MAX_NOTE]
            rows.append({
                "pair_key": pair,
                "pageid": pageid,
                "passage_key": passage["passage_key"],
                "library": location["library"],
                "repository": location["repo"],
                "declaration": location["declaration"],
                "relation": location["relation"],
                "note": note or None,
                "state": state,
                "origin": ORIGIN,
                # This offline command does not read the published store, so it
                # cannot infer what relation a community row overrides.
                "base_relation": None,
                "added_by": added_by,
                "actor_type": actor_type,
                "via": via,
                "token_id": token_id,
                "model": model,
                "run_id": run,
                "campaign": campaign,
                "revision": view["revision"],
                "record_set": record_set,
                "universe_sha256": universe_sha,
                "version": FIRST_VERSION,
                "created_at": at_ms,
                "updated_at": at_ms,
                "removed_by": None,
                "removed_at": None,
                "removed_reason": None,
                # not a column: what the reviewer wants beside the row
                "_rank": location.get("rank"),
                "_source_id": view["source_id"],
                "_item": passage["item"],
            })
    return rows, notes


def deduplicate(rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """``(kept, superseded, refusals)``: one row per ``pair_key``, later sets winning.

    Rows arrive in command-line order, so a later set's row at a pair a former set also
    named supersedes it — which is section 3.3's rule, applied to the sets this one
    invocation was given. Two rows of **one** set at one pair that disagree about the
    judgement are not a supersession and are refused: nobody can review a file that
    states two relations for one key.

    ``library`` is one of the fields compared, not only the three the key is made of.
    A ``pair_key`` names a *repository*, and a universe may split one repository into
    several libraries (lean-core's ``Init`` and ``Std``), so two locations of one
    passage can share a pair and disagree about which library it is. That is a real
    disagreement and the reviewer should see it rather than have one silently win.

    Only a collision **between** sets is counted as a supersession; an identical row
    repeated inside one set is a duplicate, and calling it a supersession would make
    the report say a set superseded itself.
    """
    by_pair: dict[str, dict] = {}
    refused_pairs: set[str] = set()
    superseded: list[dict] = []
    refusals: list[str] = []
    for row in rows:
        pair = row["pair_key"]
        if pair in refused_pairs:
            continue
        held = by_pair.get(pair)
        if held is None:
            by_pair[pair] = row
            continue
        differs = [key for key in
                   ("relation", "declaration", "repository", "library", "note")
                   if held[key] != row[key]]
        same_set = held["record_set"] == row["record_set"]
        if same_set and differs:
            refusals.append(
                f"{pair[:12]}: one set states two judgements for one pair "
                f"({'; '.join(f'{k}: {held[k]!r} and {row[k]!r}' for k in differs)}); "
                "neither is promoted")
            refused_pairs.add(pair)
            by_pair.pop(pair)
            continue
        if not same_set:
            superseded.append(held)
        by_pair[pair] = row
    kept = sorted(by_pair.values(),
                  key=lambda row: (row["pageid"], row["passage_key"], row["pair_key"]))
    return kept, superseded, refusals


# ---- the reviewable files ----------------------------------------------------------------


def check_values(rows: list[dict]) -> None:
    """Refuse a row no SQL writer should be asked to write, **before** anything is written.

    Two classes, both of which a contributor controls and neither of which a record
    set that is what it claims to be would carry:

    * a **control character** anywhere. The intake contract does not refuse one — the
      vendored checker's ``text`` is any non-empty string — so this is where it stops;
    * a **line break** in a structural name (:data:`LINE_SAFE`): a library, a
      repository, a declaration, a relation, a key, a timestamp. A line break in one
      of those ends the comment line :func:`sql_for` writes above each statement and
      makes whatever follows it a top-level SQL statement in a file the coordinator is
      told to run against the live database. ``note`` is the one field that may hold
      one, and it is never interpolated into a comment.

    Raising here rather than in the writer means the refusal reaches the command's own
    error path and exits 2, and that neither output file is written.
    """
    for row in rows:
        for name, value in sorted(row.items()):
            if name.startswith("_") or value is None or isinstance(value, (bool, int)):
                continue
            if not isinstance(value, str):
                raise PromotionProblem(
                    f"{row['pair_key'][:12]}: {name} is {value!r}, which is not a value "
                    "a row may carry")
            if _CONTROL.search(value):
                raise PromotionProblem(
                    f"{row['pair_key'][:12]}: {name} carries a control character "
                    f"({value[:60]!r}); this writer will not escape around one")
            if name in LINE_SAFE and _LINE_BREAK.search(value):
                raise PromotionProblem(
                    f"{row['pair_key'][:12]}: {name} carries a line break "
                    f"({value[:60]!r}). A {name} does not contain one, and a line break "
                    "here would end the comment above the statement and make what "
                    "follows it SQL of its own")


def comment_safe(value, *, limit: int = 120) -> str:
    """One value, safe to put after ``-- `` on one line: no break, bounded length."""
    text = _LINE_BREAK.sub(" ", _CONTROL.sub(" ", str(value)))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def sql_literal(value) -> str:
    """One SQL literal, or raise. Nothing here is a format string over user bytes.

    :func:`check_values` has already refused anything this would refuse; the checks
    stay because this function is also reachable on its own and a writer that trusts
    its caller is a writer that stops being safe when somebody adds a caller.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise PromotionProblem(f"{value!r} is not a value a row may carry")
    if _CONTROL.search(value):
        raise PromotionProblem(
            f"a value carries a control character ({value[:40]!r}); this writer will "
            "not escape around one")
    return "'" + value.replace("'", "''") + "'"


SQL_HEAD = """\
-- {promoter}
-- Overlay rows from {sets} explicitly approved record set(s), for review before application.
--
-- NOTHING HERE HAS BEEN APPLIED. This file was written offline by the coordinator; it
-- touches no D1, no binding and no network. Apply it, after reading it and after a
-- backup, the way every migration is applied (CLAUDE.md, "Site/D1"):
--
--   cd wiki && npm run backup:d1
--   cd wiki && npx wrangler d1 execute wikilean --remote --file=<this file>
--
-- The table is {table} (the public-editing plan's section 2.9, migration 0019,
-- which ships in R1). Tests execute these statements against that migration.
-- Timestamps are integer epoch milliseconds; revision comes from the accepted set.
--
-- Supersession: the ON CONFLICT clause updates a row only where its own origin is one
-- of {superseded}, via is intake, actor_type is agent, state is live, and it has not
-- been removed. Human edits and web/API edits are preserved, including human rows
-- imported earlier. The removed_* columns are never updated.
--
-- Rows: {rows}. Submitter: {added_by}. State: {state}.
BEGIN TRANSACTION;
"""

SQL_TAIL = "COMMIT;\n"


def sql_for(rows: list[dict], *, added_by: str, state: str, sets: int,
            provenance: dict[str, dict] | None = None) -> str:
    """The reviewable SQL: one upsert per row, with the supersession rule in it.

    ``provenance`` is ``{pair_key: {source_id, item, rank}}``, which is not in the table
    and is written as the comment above each statement: a reviewer reads "which passage
    of which article, and the locate run's own rank" without opening the set.
    """
    if state != STATE_LIVE or any(row.get("state") != STATE_LIVE for row in rows):
        raise PromotionProblem(
            "R1 has no pending-row table: keep this submission as review JSON; "
            "generate SQL only after explicit approval with --trusted")
    provenance = provenance or {}
    check_values(rows)
    columns = ", ".join(COLUMNS)
    assignments = ",\n    ".join(f"{name} = excluded.{name}" for name in UPDATED)
    supersedable = ", ".join(sql_literal(name) for name in SUPERSEDABLE)
    out = [SQL_HEAD.format(promoter=PROMOTER, sets=sets, table=TABLE, rows=len(rows),
                           added_by=comment_safe(added_by), state=state,
                           superseded=", ".join(repr(name) for name in SUPERSEDABLE))]
    for row in rows:
        values = ", ".join(sql_literal(row[name]) for name in COLUMNS)
        where = provenance.get(row["pair_key"]) or {}
        out.append(
            f"-- {comment_safe(where.get('source_id', '?'))} "
            f"{comment_safe(where.get('item', '?'))} "
            f"rank {comment_safe(where.get('rank'))}: "
            f"{comment_safe(row['library'])} {comment_safe(row['declaration'])} "
            f"({comment_safe(row['relation'])})\n"
            f"INSERT INTO {TABLE} ({columns})\n"
            f"VALUES ({values})\n"
            f"ON CONFLICT(pair_key) DO UPDATE SET\n    {assignments},\n"
            f"    version = {TABLE}.version + 1\n"
            f"  WHERE {TABLE}.origin IN ({supersedable})\n"
            f"    AND {TABLE}.via = 'intake' AND {TABLE}.actor_type = 'agent'\n"
            f"    AND {TABLE}.state = 'live' AND {TABLE}.removed_at IS NULL;\n")
    out.append(SQL_TAIL)
    return "\n".join(out)


def promotion(rows: list[dict], *, sets: list[dict], superseded: list[dict],
              refusals: list[str], notes: list[str], state: str,
              added_by: str) -> dict:
    """The JSON file: the rows as objects, and everything a reviewer should see first."""
    return {
        "schema": PROMOTION_SCHEMA,
        "promoter": PROMOTER,
        "table": TABLE,
        "columns": list(COLUMNS),
        "applied": False,
        "writes_to_d1": False,
        "note": "Nothing here has been applied. This file is for review.",
        "sets": sets,
        "added_by": added_by,
        "state": state,
        "supersedes_origins": list(SUPERSEDABLE),
        "supersession_requires": {"via": INTAKE_VIA, "actor_type": "agent",
                                  "state": STATE_LIVE, "removed_at": None},
        "counts": {"rows": len(rows), "superseded_within_this_run": len(superseded),
                   "refused_pairs": len(refusals), "not_promoted": len(notes)},
        "refused": refusals,
        "not_promoted": notes,
        "rows": [{name: row[name] for name in COLUMNS} for row in rows],
        # not columns: where each row came from, so a reviewer can find the passage
        "provenance": {row["pair_key"]: {"source_id": row["_source_id"],
                                         "item": row["_item"], "rank": row["_rank"]}
                       for row in rows},
    }


# ---- the command --------------------------------------------------------------------------


def promote(set_dirs: list[Path], *, added_by: str, created_at: str,
            machine_id: str = "", trusted: bool = False, actor_type: str = "agent",
            via: str = INTAKE_VIA, token_id: str | None = None,
            model: str | None = None, run_id: str | None = None) -> dict:
    """Verify every set through the intake contract, then build the promotion."""
    if actor_type not in ACTOR_TYPES:
        raise PromotionProblem(f"--actor-type is one of {', '.join(ACTOR_TYPES)}, "
                               f"not {actor_type!r}")
    timestamp_ms(created_at)
    if via != INTAKE_VIA:
        raise PromotionProblem("--via must be intake; imported sets cannot impersonate web or API edits")
    if not added_by:
        raise PromotionProblem("--added-by names the account the rows are attributed to; "
                               "use the actual author's account, including the agent id "
                               "for an agent's mapping")
    state = STATE_LIVE if trusted else STATE_PENDING
    validate, schema_error, _ = build_store.schema_validator()
    rows: list[dict] = []
    notes: list[str] = []
    sets: list[dict] = []
    for set_dir in set_dirs:
        # the intake contract first: a set this refuses is not a set to promote, and
        # running it here means the promoter cannot be the way an unchecked set gets in
        outcome = intake.verify(Path(set_dir),
                                scope=intake.KeyScope(machine_id=machine_id),
                                received_at=created_at)
        if isinstance(outcome, intake.Refusal):
            raise PromotionProblem(
                f"{set_dir} is not an accepted submission, so there is nothing to "
                f"promote:\n{outcome.report()}")
        built = build_store.build_set(Path(set_dir), validate, schema_error)
        sets.append({"set_dir": Path(set_dir).name,
                     "set_id": outcome.submission["set_id"],
                     "seal_sha256": outcome.submission["seal_sha256"],
                     "campaign": outcome.submission["campaign"],
                     "universe_sha256": outcome.submission["universe_sha256"],
                     "status_rule": outcome.submission["status_rule"],
                     "skill_commit": outcome.submission["skill_commit"],
                     "records": len(outcome.records)})
        for view, _records in built.articles:
            made, said = rows_of_article(
                view, state=state, added_by=added_by, actor_type=actor_type, via=via,
                token_id=token_id, model=model, run_id=run_id, created_at=created_at)
            rows.extend(made)
            notes.extend(said)
    kept, superseded, refusals = deduplicate(rows)
    check_values(kept)
    return promotion(kept, sets=sets, superseded=superseded, refusals=refusals,
                     notes=notes, state=state, added_by=added_by)


def describe(doc: dict) -> str:
    counts = doc["counts"]
    lines = [f"{counts['rows']} overlay row(s) from {len(doc['sets'])} set(s), "
             f"state {doc['state']}, attributed to {doc['added_by']}"]
    if counts["superseded_within_this_run"]:
        lines.append(f"  {counts['superseded_within_this_run']} row(s) superseded by a "
                     "later set in this run")
    for text in doc["refused"]:
        lines.append(f"  REFUSED: {text}")
    for text in doc["not_promoted"][:10]:
        lines.append(f"  not promoted: {text}")
    if len(doc["not_promoted"]) > 10:
        lines.append(f"  ... and {len(doc['not_promoted']) - 10} more")
    lines.append("  nothing was written to D1")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record-set", required=True, action="append", type=Path,
                        dest="record_sets", metavar="DIR",
                        help="an accepted submission (repeatable; a later one supersedes "
                             "an earlier one at a shared pair_key)")
    parser.add_argument("--added-by", required=True,
                        help="the mapping author's account id; use the agent id "
                             "for an agent's output, not its human owner")
    parser.add_argument("--created-at", required=True,
                        help="the time the rows carry, as ISO 8601 UTC ending in Z; "
                             "passed in so the output is a function of its inputs")
    parser.add_argument("--machine-id", default="",
                        help="the machine the submission's key is bound to, recorded on "
                             "the staging row the verifier builds")
    parser.add_argument("--trusted", action="store_true",
                        help="explicitly approve these rows for live SQL; without it "
                             "only pending review JSON may be generated")
    parser.add_argument("--actor-type", default="agent", choices=ACTOR_TYPES)
    parser.add_argument("--via", default=INTAKE_VIA, choices=[INTAKE_VIA],
                        help="imports are attributed as intake, never web or API edits")
    parser.add_argument("--token-id", default=None,
                        help="the machine key the set was submitted with")
    parser.add_argument("--model", default=None, help="the model that produced the set")
    parser.add_argument("--run-id", default=None,
                        help="the locate run (default: read off the campaign token)")
    parser.add_argument("--sql", type=Path, help="write the reviewable SQL here")
    parser.add_argument("--json", type=Path, dest="json_out",
                        help="write the rows as JSON here")
    parser.add_argument("--report", type=Path,
                        help="write what was and was not promoted here (default: stdout)")
    args = parser.parse_args(argv)

    if not args.sql and not args.json_out:
        print("promote: name --sql, --json, or both; this command's whole output is a "
              "file somebody reads before anything is applied", file=sys.stderr)
        return 2
    problem = build_store.interpreter_problem()
    if problem is not None:
        print(f"promote: {problem}; nothing was promoted", file=sys.stderr)
        return 3
    # Both files are rendered inside the try and written after it, so a row the SQL
    # writer refuses leaves neither file behind rather than a .json without its .sql.
    try:
        doc = promote(args.record_sets, added_by=args.added_by,
                      created_at=args.created_at, machine_id=args.machine_id,
                      trusted=args.trusted, actor_type=args.actor_type, via=args.via,
                      token_id=args.token_id, model=args.model, run_id=args.run_id)
        sql = (sql_for(doc["rows"], added_by=doc["added_by"], state=doc["state"],
                       sets=len(doc["sets"]), provenance=doc["provenance"])
               if args.sql else None)
    except PromotionProblem as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except build_store.ArticlesError as exc:
        print(f"promote: the set was refused: {exc}", file=sys.stderr)
        return 2
    if args.sql:
        args.sql.parent.mkdir(parents=True, exist_ok=True)
        args.sql.write_text(sql, encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")
    text = describe(doc)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 1 if (doc["refused"] or doc["not_promoted"]) else 0


if __name__ == "__main__":
    sys.exit(main())
