#!/usr/bin/env python3
"""Stage 9d: which records a challenge judges, and the plan it writes.

docs/PIPELINE.md section 3 (stage 9d). Ported from formal-companion's
``scripts/locate/challenge_plan.py``; the verdict **vocabulary** is already WikiLean's
(:mod:`vocab`) and the verdict **semantics** are already WikiLean's (:mod:`tally`, proved
on batch 1's real verdicts). What is here is the third piece: the plan — which records
to require, which to draw, and which to leave alone.

Over the outcome rows of one or more harvested locate runs:

* every **location on a subject or central passage** is refuted by default — required;
* every **absence and unresolved outcome** on a subject or central passage is **hunted**
  — required: a challenger searches rather than argues;
* every **coverage claim**, whatever the passage's prominence, is refuted by default: it
  upgrades the passage's derived status, so no claim leaves unchallenged;
* every **strong location whose own note admits a clause it does not state**
  (``gate.verify_locate.admits_missing_clause``) is refuted by default too, whatever its
  passage's prominence: a relation that says the opposite of itself is not evidence;
* every **subject passage's prominence** is refuted by default and never sampled: the
  article's own identity rides on every record of a subject passage, so a passage
  mislabelled ``subject`` would keep it with no verdict that could take it away;
* on a supporting passage, every location that sets the passage's derived status **on
  its own** — its *bases* (:func:`bases`) — is required: the passage keeps its status
  while any one of them stands, so a correct downgrade of the best-ranked one alone
  changes nothing. Under plan rule 1 only a *strong* basis was required, and a weak one
  that set the status alone fell to its relation's stratum at ``sample_rate``; under
  rule 2 (issue #175) a basis is required whatever its relation;
* a **stratified, seeded sample** of the rest: one stratum per scope a passage was
  demoted to, one for the supporting passages' prominences, one per relation on the
  remaining locations, and one of the supporting passages' absences. Each stratum takes
  ``ceil(rate × size)`` by exact arithmetic — 0.07 of 100 records is 7, never the 8 a
  float product rounds to — so a non-empty stratum at a rate above zero always draws at
  least one. What is not drawn is ``sampled_out``;
* everything else is ``not_required``.

The draw is a function of the seed and the record keys alone (each record's rank is
``sha256(seed, key)``), so re-planning an unchanged run gives the same plan.

**The plan rule.** Which of those rules a plan was drawn under is
``challenge-plan.json``'s ``plan_rule``: 1 the rules the first runs were planned with,
2 issue #175's. The default of :func:`plan` is 1, so a caller that names no rule draws
the plan its code was written against; ``--plan-rule`` defaults to 2, and a replay of an
older plan passes ``--plan-rule 1``.

**Conditional records.** A prominence sample that overturns a supporting passage to
central reopens records no task held, which would stay owed forever. So every location,
absence and unresolved record of a supporting passage whose prominence record is
required, and which the plan did not require itself, joins that passage's task marked
``conditional: "prominence"`` with its planned value unchanged
(:func:`mark_conditional`). The challenger judges them only if it overturns the
prominence, and :func:`tally.apply_challenge` applies such a verdict only then — the
reader of this key is already built and proved.

**A passage is one unit.** Its records go to one task, so one challenger judges a
coverage claim and the parts it names, and every required location of a passage: two
encodings of one object, or a definition and the declaration defined from it, get one
challenger's relations.

    python3 scripts/pipeline/challenge.py --rows <outcomes.jsonl> --run-id chal-1 \\
        --out <dir> [--seed 0] [--sample-rate 0.2] [--plan-rule 2]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import keys as keys_mod  # noqa: E402
import status as status_mod  # noqa: E402
import tally as tally_mod  # noqa: E402
import verify_locate  # noqa: E402 — scripts/pipeline/gate/verify_locate.py
import vocab  # noqa: E402

REFUTE = vocab.CHALLENGE_RULE_REFUTE
HUNT = vocab.CHALLENGE_RULE_HUNT
BASIS = vocab.CHALLENGE_RULE_BASIS
SAMPLE = vocab.CHALLENGE_RULE_SAMPLE
NONE = vocab.CHALLENGE_RULE_NONE
#: the stratum of the supporting passages' absences
ABSENCE_STRATUM = f"outcome:{vocab.OUTCOME_ABSENT}"
#: the stratum of the supporting passages' prominences
SUPPORTING_STRATUM = f"prominence:{vocab.PROMINENCE_SUPPORTING}"
#: the stratum a subject passage's prominence record is named by: never drawn
SUBJECT_STRATUM = f"prominence:{vocab.PROMINENCE_SUBJECT}"
CONDITIONAL = tally_mod.CONDITIONAL_PROMINENCE
#: the records a required prominence sample takes into its task as conditional ones
CONDITIONAL_TARGETS = ("location", "absence", "unresolved")
#: what a coverage record names of each of its parts, for the challenger
PART_KEYS = ("location_key", "library", "declaration", "relation", "rank")
#: what it names beside them on a run that reads bundles
BUNDLE_PART_KEYS = ("clauses", "conjunct_relation", "pd6")
#: what a record copies from a bundle-reading outcome row
STATEMENT_KEYS = ("statement", "conjuncts", "shape")
#: what a location record shows of each other location on its passage (read-only)
SIBLING_KEYS = ("location_key", "library", "declaration", "relation", "rank", "planned")
#: the keys ``challenge-plan.json`` writes for every record, in order
PLAN_ROW_KEYS = ("key", "target", "source_id", "item", "location_key", "library",
                 "declaration", "relation", "outcome", "run_id", "prominence", "scope",
                 "rule", "stratum", "planned")


class ChallengeProblem(ValueError):
    """Outcome rows a challenge cannot be planned from."""


def _rank(seed: int, key: str) -> str:
    return keys_mod.sha256_text(f"{int(seed)}\n{key}")


def _by_rank(location: dict) -> tuple:
    return (location.get("rank") or 99, str(location.get("library")),
            str(location.get("declaration")))


def bases(row: dict, status_rule: int = vocab.STATUS_RULE_D1) -> list[dict]:
    """Every location that sets a passage's derived status on its own, best first.

    Each location whose own status — :func:`status.location_status` over it alone — is
    the passage's pre-challenge status: the passage keeps that status while any one of
    them stands, so a correct downgrade of the best-ranked one alone changes nothing. A
    passage with no status, or whose locations were not joined to the declaration index
    (no ``proof_status``), has none. No coverage claim is ever a basis: an unchallenged
    claim upgrades nothing, and every claim is refuted by default anyway.

    ``status_rule`` is the rule the *location's own* status is read under. It is the
    status rule the plan is made with and not necessarily the one the passage's status
    was derived under: what the rule decides here is only which locations the plan
    requires, never a published value.
    """
    wanted = row.get("status")
    if wanted is None:
        return []
    kind = row.get("passage_kind") or "other"
    out = []
    for loc in sorted(row.get("locations") or [], key=_by_rank):
        if loc.get("relation") == vocab.RELATED_MATCH or not loc.get("proof_status"):
            continue
        alone = {"relation": loc["relation"], "proof_status": loc["proof_status"],
                 "statement": bool(loc.get("statement"))}
        try:
            own = status_mod.location_status(kind, alone, status_rule)
        except (status_mod.StatusProblem, KeyError):
            continue
        if own == wanted:
            out.append(loc)
    return out


#: what a record shows of the passage beyond the outcome row: the inventory's own
#: quote and reading-copy span, which the tally's rows do not carry (they are the cut's,
#: not the tally's) and which a challenger needs to find the passage in ``text.txt``
PASSAGE_KEYS = ("quote", "text_span")


def _record(row: dict, target: str, *, run_id: str, location: dict | None = None,
            status_rule: int = vocab.STATUS_RULE_D1,
            passage: dict | None = None) -> dict:
    passage = passage or {}
    rec = {"target": target, "source_id": row["source_id"], "item": row["item"],
           "run_id": run_id, "prominence": row.get("prominence"),
           "scope": row.get("scope"), "passage_kind": row.get("passage_kind"),
           "outcome": row.get("outcome"), "span": row.get("span"),
           "section": row.get("section")}
    rec.update({key: passage.get(key) for key in PASSAGE_KEYS})
    if row.get("claims") is not None:
        rec["claims"] = row["claims"]
    if vocab.reads_bundles(status_rule):
        rec.update({key: row[key] for key in STATEMENT_KEYS if row.get(key) is not None})
    if location is not None:
        rec.update({"location_key": location["location_key"],
                    "library": location["library"],
                    "declaration": location["declaration"],
                    "relation": location["relation"], "rank": location.get("rank"),
                    "note": location.get("note") or None})
        rec.update({key: location[key] for key in vocab.JUDGMENT_LOCATION_FIELDS
                    if key in location})
    rec["key"] = tally_mod.record_key(target, row["source_id"], row["item"],
                                      rec.get("location_key"))
    return rec


def _location_rule(loc: dict, *, hot: bool, based: list[dict],
                   plan_rule: int = vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS
                   ) -> tuple[str, str | None]:
    """``(rule, stratum)`` of one location record."""
    relation = loc["relation"]
    if hot:
        return REFUTE, None
    if relation in vocab.STRONG_RELATIONS and verify_locate.admits_missing_clause(
            loc.get("note")):
        # its own note says it leaves a clause unstated: a strong relation that says the
        # opposite of itself is refuted by default, whatever the passage's prominence
        return REFUTE, None
    if any(loc is b for b in based) and (
            relation in vocab.STRONG_RELATIONS
            or plan_rule >= vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS):
        # a basis sets the passage's status on its own, whatever its relation: a
        # hasPart that alone makes a passage partial is as load-bearing as a strong
        # location that alone makes it formalized (issue #175)
        return BASIS, None
    if relation in vocab.WEAK_RELATIONS or relation in vocab.STRONG_RELATIONS:
        return SAMPLE, f"relation:{relation}"
    return NONE, None


def classify(rows: Iterable[dict], *, run_id: str,
             status_rule: int = vocab.STATUS_RULE_D1,
             basis_rule: int = vocab.STATUS_RULE_D1,
             plan_rule: int = vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS,
             passages: dict | None = None) -> list[dict]:
    """Every record of one run's outcome rows, each with its rule and stratum.

    ``passages`` is ``{(source_id, item): inventory passage}``: where a challenger
    reads the passage from, which the outcome rows do not say. ``plan_rule`` is which
    set of planning rules the draw is made under (:data:`vocab.CHALLENGE_PLAN_RULES`).
    """
    bundle = vocab.reads_bundles(status_rule)
    passages = passages or {}
    out: list[dict] = []
    for row in rows:
        if row.get("missing"):
            continue
        here = passages.get((row["source_id"], row["item"]))
        hot = row.get("prominence") in vocab.REFUTE_BY_DEFAULT_PROMINENCE
        based = [] if hot else bases(row, basis_rule)
        for loc in row.get("locations") or []:
            rec = _record(row, "location", run_id=run_id, location=loc,
                          status_rule=status_rule, passage=here)
            rule, stratum = _location_rule(loc, hot=hot, based=based,
                                           plan_rule=plan_rule)
            rec.update(rule=rule, stratum=stratum)
            out.append(rec)
        claim = row.get("coverage")
        if claim:
            rec = _record(row, vocab.COVERAGE_TARGET, run_id=run_id,
                          status_rule=status_rule, passage=here)
            by_key = {loc["location_key"]: loc for loc in row.get("locations") or []}
            rec["parts"] = [
                {k: by_key.get(key, {}).get(k) for k in PART_KEYS}
                | ({k: by_key[key][k] for k in BUNDLE_PART_KEYS
                    if by_key.get(key, {}).get(k) is not None} if bundle else {})
                | {"location_key": key} for key in claim["parts"]]
            rec["note"] = claim.get("note")
            rec.update(rule=REFUTE, stratum=None)
            out.append(rec)
        outcome = row.get("outcome")
        if outcome in (vocab.OUTCOME_ABSENT, vocab.OUTCOME_UNRESOLVED):
            target = "absence" if outcome == vocab.OUTCOME_ABSENT else "unresolved"
            rec = _record(row, target, run_id=run_id, status_rule=status_rule,
                          passage=here)
            if hot:
                rec.update(rule=HUNT, stratum=None)
            elif outcome == vocab.OUTCOME_ABSENT:
                rec.update(rule=SAMPLE, stratum=ABSENCE_STRATUM)
            else:
                rec.update(rule=NONE, stratum=None)
            out.append(rec)
        elif outcome is None:
            rec = _record(row, "scope", run_id=run_id, status_rule=status_rule,
                          passage=here)
            rec.update(rule=SAMPLE, stratum=f"scope:{row.get('scope')}")
            out.append(rec)
        if row.get("prominence") == vocab.PROMINENCE_SUPPORTING:
            rec = _record(row, "prominence", run_id=run_id, status_rule=status_rule,
                          passage=here)
            rec.update(rule=SAMPLE, stratum=SUPPORTING_STRATUM)
            out.append(rec)
        elif row.get("prominence") == vocab.PROMINENCE_SUBJECT:
            # the article's identity rests on this label: required, whatever the rates
            rec = _record(row, "prominence", run_id=run_id, status_rule=status_rule,
                          passage=here)
            rec.update(rule=REFUTE, stratum=SUBJECT_STRATUM)
            out.append(rec)
    return out


def rates_of(sample_rate: float = vocab.CHALLENGE_SAMPLE_RATE,
             strong_sample_rate: float = vocab.CHALLENGE_STRONG_SAMPLE_RATE,
             absence_sample_rate: float = vocab.CHALLENGE_ABSENCE_SAMPLE_RATE
             ) -> dict[str, float]:
    """The plan's three rates by their :data:`vocab.RATE_KEYS` names, each in [0, 1]."""
    rates = {"sample_rate": sample_rate, "strong_sample_rate": strong_sample_rate,
             "absence_sample_rate": absence_sample_rate}
    for name, rate in rates.items():
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) \
                or not 0 <= rate <= 1:
            raise ChallengeProblem(f"the {name.replace('_', ' ')} is a fraction in "
                                   f"[0, 1], not {rate!r}")
    return rates


#: Which relations draw at ``strong_sample_rate``, by plan rule. Rules 1 draw the three
#: strong ones; rules 2 add ``hasPart`` and ``narrowMatch`` on a supporting passage —
#: issue #175 found 18 of its 22 wrong partial links sampled out at ``sample_rate``,
#: and a partial link sets a passage's status as surely as a strong one.
STRONG_RATE_RELATIONS = {
    vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS: frozenset(vocab.STRONG_RELATIONS),
    vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS: frozenset(vocab.STRONG_RELATIONS
                                                    | vocab.PART_RELATIONS),
}


def stratum_rate(stratum: str, rates: dict[str, float],
                 plan_rule: int = vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS) -> float:
    """One stratum's rate: the strong relations', the absences', else the plan's."""
    if stratum == ABSENCE_STRATUM:
        return rates["absence_sample_rate"]
    relations = STRONG_RATE_RELATIONS.get(plan_rule) or frozenset()
    if stratum.startswith("relation:") and stratum.split(":", 1)[1] in relations:
        return rates["strong_sample_rate"]
    return rates["sample_rate"]


def draw(records: list[dict], *, seed: int,
         plan_rule: int = vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS,
         **rate_args) -> list[dict]:
    """Give every record its planned value: required, sampled_out or not_required."""
    rates = rates_of(**rate_args)
    strata: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        if rec["rule"] in (REFUTE, HUNT, BASIS):
            rec["planned"] = vocab.CHALLENGE_REQUIRED
        elif rec["rule"] == SAMPLE:
            strata[rec["stratum"]].append(rec)
        else:
            rec["planned"] = vocab.CHALLENGE_NOT_REQUIRED
    for stratum, members in sorted(strata.items()):
        members.sort(key=lambda r: _rank(seed, r["key"]))
        rate = stratum_rate(stratum, rates, plan_rule)
        # exact arithmetic: 0.07 of 100 records is 7, never the 8 a float product
        # rounds to
        take = math.ceil(Fraction(str(rate)) * len(members)) if rate > 0 else 0
        for i, rec in enumerate(members):
            rec["planned"] = (vocab.CHALLENGE_REQUIRED if i < take
                              else vocab.CHALLENGE_SAMPLED_OUT)
    return records


def mark_conditional(records: list[dict]) -> list[dict]:
    """Mark each record a required prominence sample takes into its task."""
    sampled = {(r["run_id"], r["source_id"], r["item"]) for r in records
               if r["target"] == "prominence"
               and r["planned"] == vocab.CHALLENGE_REQUIRED
               and r.get("prominence") == vocab.PROMINENCE_SUPPORTING}
    marked = []
    for rec in records:
        if (rec["target"] in CONDITIONAL_TARGETS
                and rec["planned"] != vocab.CHALLENGE_REQUIRED
                and (rec["run_id"], rec["source_id"], rec["item"]) in sampled):
            rec["conditional"] = CONDITIONAL
            marked.append(rec)
    return marked


def show_siblings(records: list[dict]) -> None:
    """Give each location record the passage's other locations, read-only.

    A challenger keeps definitionally linked declarations at one relation; the ones its
    task does not judge are context, never records it gives a verdict for.
    """
    by_passage: dict[tuple, list[dict]] = defaultdict(list)
    for rec in records:
        if rec["target"] == "location":
            by_passage[(rec["run_id"], rec["source_id"], rec["item"])].append(rec)
    for members in by_passage.values():
        members.sort(key=_by_rank)
        for rec in members:
            rec["passage_locations"] = [{k: other.get(k) for k in SIBLING_KEYS}
                                        for other in members if other is not rec]


def _in_task(rec: dict) -> bool:
    """A record a challenge task holds: a required one, or a conditional one."""
    return (rec["planned"] == vocab.CHALLENGE_REQUIRED
            or rec.get("conditional") == CONDITIONAL)


def batches(records: list[dict], *, size: int = vocab.CHALLENGE_BATCH_RECORDS
            ) -> list[list[dict]]:
    """The records a task holds, in batches of about ``size``, a passage never split."""
    if size < 1:
        raise ChallengeProblem(f"a task holds at least one record, not {size}")
    required = sorted((r for r in records if _in_task(r)),
                      key=lambda r: (r["source_id"], (r.get("span") or [0])[0], r["item"],
                                     r["target"], r["key"]))
    units: list[list[dict]] = []
    for rec in required:
        passage = (rec["source_id"], rec["item"])
        if units and (units[-1][0]["source_id"], units[-1][0]["item"]) == passage:
            units[-1].append(rec)
        else:
            units.append([rec])
    out: list[list[dict]] = []
    for unit in units:
        if not out or len(out[-1]) + len(unit) > size:
            out.append([])
        out[-1].extend(unit)
    return out


def task_id(number: int) -> str:
    """``chl-0001``: a challenge task is named by its number, not by a source."""
    return f"chl-{number:04d}"


def plan(rows_by_run: dict[str, list[dict]], *, seed: int = 0,
         status_rule: int = vocab.STATUS_RULE_D1,
         basis_rule: int = vocab.STATUS_RULE_D1,
         plan_rule: int = vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS,
         passages: dict | None = None,
         batch_size: int = vocab.CHALLENGE_BATCH_RECORDS, conditional: bool = True,
         **rate_args) -> dict:
    """The whole plan: every record classified, drawn, marked, and batched into tasks."""
    rates = rates_of(**rate_args)
    if plan_rule not in vocab.CHALLENGE_PLAN_RULES:
        raise ChallengeProblem(f"a plan is drawn under one of "
                               f"{list(vocab.CHALLENGE_PLAN_RULES)}, not {plan_rule!r}")
    if not rows_by_run:
        raise ChallengeProblem("a challenge plan judges harvested locate runs; name at "
                               "least one")
    records: list[dict] = []
    for run_id in sorted(rows_by_run):
        records.extend(classify(rows_by_run[run_id], run_id=run_id,
                                status_rule=status_rule, basis_rule=basis_rule,
                                plan_rule=plan_rule, passages=passages))
    seen: dict[str, str] = {}
    for rec in records:
        other = seen.get(rec["key"])
        if other is not None:
            raise ChallengeProblem(
                f"record {rec['key'][:12]} ({rec['source_id']} {rec['item']}, "
                f"{rec['target']}) appears twice"
                + (f", in runs {other} and {rec['run_id']}" if other != rec["run_id"]
                   else ""))
        seen[rec["key"]] = rec["run_id"]
    draw(records, seed=seed, plan_rule=plan_rule, **rates)
    if conditional:
        mark_conditional(records)
    show_siblings(records)
    groups = batches(records, size=batch_size)
    task_of: dict[str, str] = {}
    for number, group in enumerate(groups, 1):
        for rec in group:
            task_of[rec["key"]] = task_id(number)
    return {"records": records, "batches": groups, "task_of": task_of, "rates": rates,
            "seed": int(seed), "plan_rule": int(plan_rule)}


def plan_document(run_id: str, built: dict, *, challenged_runs: Iterable[str]) -> dict:
    """``challenge-plan.json``: every record, its rule, stratum, planned value and task.

    The tally reads this back: a record a plan required and no verdict judged is owed,
    never silently ``not_required``.
    """
    task_of = built["task_of"]
    rows = []
    for rec in sorted(built["records"], key=lambda r: (r["source_id"], r["item"],
                                                       r["target"], r["key"])):
        row = {key: rec.get(key) for key in PLAN_ROW_KEYS}
        row["task_id"] = task_of.get(rec["key"])
        if rec["target"] == vocab.COVERAGE_TARGET:
            row["parts"] = [part["location_key"] for part in rec["parts"]]
        if rec.get("conditional"):
            row["conditional"] = rec["conditional"]
        rows.append(row)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["planned"]] += 1
    return {"schema": vocab.CHALLENGE_PLAN_SCHEMA, "run_id": run_id,
            "challenged_runs": sorted(challenged_runs), "seed": built["seed"],
            "plan_rule": built.get("plan_rule",
                                   vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS),
            **built["rates"], "counts": dict(sorted(counts.items())), "records": rows}


#: what a task manifest names of each source whose records it holds
SOURCE_PINS = ("source_sha", "layer_sha256", "renderer")
#: the plan's own bookkeeping, which a task manifest leaves out: a challenger is told
#: what to judge, never why the plan picked it or how it was drawn
PLAN_ONLY_KEYS = frozenset({"rule", "stratum", "planned"})


def task_documents(run_id: str, built: dict, sources: dict[str, dict] | None = None
                   ) -> list[dict]:
    """One task manifest per batch, as the worker and the gate read it.

    A batch's records are grouped into one document per source, each carrying the
    source's pins, so the gate checks a verdict against the bytes the locate run read.
    """
    sources = sources or {}
    out = []
    for number, group in enumerate(built["batches"], 1):
        documents: list[dict] = []
        for rec in group:
            sid = rec["source_id"]
            if not documents or documents[-1]["source_id"] != sid:
                pins = sources.get(sid) or {}
                documents.append({"source_id": sid,
                                  **{key: pins.get(key) for key in SOURCE_PINS},
                                  "records": []})
            documents[-1]["records"].append(
                {k: v for k, v in rec.items()
                 if v is not None and k != "source_id" and k not in PLAN_ONLY_KEYS})
        out.append({"task_id": task_id(number), "run_id": run_id,
                    "mode": vocab.MODE_CHALLENGE, "documents": documents})
    return out


def read_rows(path: Path) -> list[dict]:
    """A locate run's outcome rows, one JSON object per line."""
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise ChallengeProblem(f"{path} line {number}: not JSON ({exc})") from None
        if not isinstance(value, dict):
            raise ChallengeProblem(f"{path} line {number}: not an object")
        rows.append(value)
    return rows


def read_sources(path: Path) -> dict[str, dict]:
    """``{source_id: {source_sha, layer_sha256, renderer}}`` of a sourceset manifest."""
    out: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict) and entry.get("source_id"):
            out[str(entry["source_id"])] = {"source_sha": entry.get("html_sha256"),
                                            "layer_sha256": entry.get("layer_sha256"),
                                            "renderer": entry.get("renderer")}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="the challenge run's id")
    parser.add_argument("--rows", action="append", default=[], required=True,
                        metavar="RUN=FILE",
                        help="a locate run's outcome rows: <run-id>=<outcomes.jsonl>")
    parser.add_argument("--out", required=True, type=Path,
                        help="where challenge-plan.json and tasks/ are written")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--sample-rate", type=float, default=vocab.CHALLENGE_SAMPLE_RATE)
    parser.add_argument("--strong-sample-rate", type=float,
                        default=vocab.CHALLENGE_STRONG_SAMPLE_RATE)
    parser.add_argument("--absence-sample-rate", type=float,
                        default=vocab.CHALLENGE_ABSENCE_SAMPLE_RATE)
    parser.add_argument("--status-rule", type=int, default=vocab.STATUS_RULE_BROAD)
    parser.add_argument("--plan-rule", type=int,
                        default=vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS,
                        help="which planning rules to draw under "
                             f"({', '.join(map(str, vocab.CHALLENGE_PLAN_RULES))}); "
                             "a replay of an older plan passes the rule it was drawn "
                             "with")
    parser.add_argument("--batch-size", type=int, default=vocab.CHALLENGE_BATCH_RECORDS)
    parser.add_argument("--manifest", type=Path,
                        help="the sourceset manifest the locate runs pinned, for the "
                             "source pins a task manifest names")
    parser.add_argument("--no-conditional", action="store_true",
                        help="do not mark the conditional records")
    args = parser.parse_args(argv)
    try:
        rows_by_run: dict[str, list[dict]] = {}
        for spec in args.rows:
            run, _, where = spec.partition("=")
            if not run or not where:
                raise ChallengeProblem(f"--rows takes <run-id>=<file>, not {spec!r}")
            rows_by_run[run] = read_rows(Path(where))
        built = plan(rows_by_run, seed=args.seed, status_rule=args.status_rule,
                     plan_rule=args.plan_rule,
                     batch_size=args.batch_size, conditional=not args.no_conditional,
                     sample_rate=args.sample_rate,
                     strong_sample_rate=args.strong_sample_rate,
                     absence_sample_rate=args.absence_sample_rate)
        document = plan_document(args.run_id, built, challenged_runs=rows_by_run)
        sources = read_sources(Path(args.manifest)) if args.manifest else {}
    except (ChallengeProblem, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = Path(args.out)
    (out / "tasks").mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    (out / "challenge-plan.json").write_text(text, encoding="utf-8")
    for manifest in task_documents(args.run_id, built, sources):
        body = json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        (out / "tasks" / f"{manifest['task_id']}.json").write_text(body, encoding="utf-8")
    print(f"{args.run_id}: {len(document['records'])} record(s), "
          f"{document['counts'].get(vocab.CHALLENGE_REQUIRED, 0)} required, "
          f"{len(built['batches'])} task(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
