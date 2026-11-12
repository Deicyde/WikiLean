#!/usr/bin/env python3
"""Stage 8: harvested documents to outcome rows, under the challenge's verdicts.

docs/PIPELINE.md section 3 (stage 8). This is the half that was missing between a run
and a sealed set: stages 1 to 6 can read, check, re-derive and re-seal a record set, and
this is what **makes** the rows they seal, out of the documents a run brought home.

Three steps, in this order, each a function of what landed:

1. **Finding rows.** Every document becomes the rows the finding table names — one
   ``PASSAGE`` per inventory passage, one ``LOCATION`` per location, one ``ABSENCE`` per
   checked absence, one ``UNRESOLVED`` per unresolved passage or document — keyed by
   :func:`keys.finding_id`, with the library id as the ``repo`` and
   ``<library>:<declaration>@<commit>`` the declaration list.
2. **Challenge.** The verdicts of the challenge runs given are applied: ``confirm`` and
   ``uphold`` keep a record and make it ``survived``; ``downgrade``, ``refute`` and
   ``overturn`` change it and make the affected records ``downgraded``, except that a
   location an overturn adds is the challenger's own witnessed finding and stands. A
   passage's coverage claim is its own record: ``confirm`` makes it ``survived`` and
   confirms its parts, ``refute`` removes it and leaves its parts plain ``hasPart``
   locations. A record the plan did not require keeps the value the plan recorded, with
   two exceptions — a part of a confirmed claim is ``survived``, and a prominence
   overturn to ``subject`` or ``central`` puts a record under a rule that requires one,
   which makes it ``challenge_pending``. **The reopen wins**: it is applied first, and a
   part it made pending stays pending, owed its own verdict under the stricter rule,
   while the claim itself stays ``survived``. A record the plan required and no verdict
   judged has challenge ``None`` — owed, which the export refuses.
3. **Status.** ``proof_status`` and the ``statement`` flag are joined from the
   declaration index (:mod:`index`, never read from a document), and :func:`status.derive`
   computes the passage's status over the surviving locations, the absence and the
   coverage claim. An unresolved passage has no status and is counted on its own, never
   as not formalized.

A passage the run planned but no document carries is kept with ``outcome: None`` and
``missing: true``: the safety metric counts it as a miss, so dropping it here would hide
one. A challenge run is applied only to the locate run it challenged, only to the records
its plan REQUIRED, and only while each record is what the plan saw; anything else is
named in ``problems`` and left owed.

**An unchallenged run** (``--unchallenged``, never beside a challenge run) is sealed with
no challenge at all: every record a challenge would have judged is ``not_required``,
which the pages read as unchallenged, so nothing is owed and a coverage claim upgrades
nothing. It is how a contributor's set reaches the intake without a stage-9d run.

**Status rule 3** is what WikiLean's own runs are derived under, and the only one batch 1
uses. Under it (and under rule 2, which shares its shapes) a coverage claim is a
**bundle** whose relation is its weakest part's (:func:`bundle_relation`); a challenge
document's ``fills`` replace the last counting location a verdict removed; a ``located``
passage the challenge leaves with no counting location and no absence is **owed a hunt**;
and the surviving cards are ranked by relation strength. A run of status rule 1 derives
exactly the rows it always did, whatever keys its documents carry.

Ported from ``locate/outcomes.py`` and the ``--locate`` branch of ``cfcmd/tally.py``.
What changes in the move: there is no workspace object — a run is a corpus directory and
a run directory, named on the command line; the universe is the ``universe.json`` shape
stage 6 already writes; the proof-status join is :mod:`index`, a stated input, rather
than a database the producer happens to hold; and the status half is :mod:`status`
(stage 3), which was already here.

    python3 scripts/pipeline/tally.py --run-id loc-1 --corpus <dir> --runs <dir> \\
        --universe <file> --index <file> [--challenge-run chl-1 | --unchallenged] \\
        [--manifest <file>] [--sources <dir>] \\
        [--outcomes <file>] [--report <file>]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import corpus as corpus_mod  # noqa: E402
import index as index_mod  # noqa: E402
import items  # noqa: E402
import keys  # noqa: E402
import records as records_mod  # noqa: E402
import status as status_mod  # noqa: E402
import vocab  # noqa: E402

#: The report this module writes beside the outcome rows.
TALLY_SCHEMA = "wikilean.pipeline.tally/1"
#: What a row of status rule 2 or 3 copies from its frozen inventory passage.
STATEMENT_FIELDS = ("statement", "conjuncts", "shape")
#: The location fields judgment rules 3 added: kept from a document, and taken from a
#: verdict, only on a run whose status rule reads bundles, so a run of status rule 1
#: yields the rows it always did whatever keys its documents carry.
CUT_LOCATION_FIELDS = ("conjunct_relation", "tests", "pd6")
#: The challenge target a conditional record waits on: its verdict applies once the same
#: challenge run overturns that passage's prominence to subject or central.
CONDITIONAL_PROMINENCE = "prominence"
#: What a confirm or downgrade may say of the record it leaves, replacing the worker's.
VERDICT_FIELDS = ("step", "convention", "instantiation")
RELATION_STRENGTH = vocab.RELATION_STRENGTH


class TallyProblem(ValueError):
    """An input the tally refuses rather than count anyway."""


# ---- the run's files -------------------------------------------------------------------


class Workspace:
    """Where a run's files are: ``corpus/<run>/`` and ``runs/<run>/``.

    No workspace object travels with a run in this pipeline: these are two directories
    an operator names, and everything under them is read by its documented name.
    """

    def __init__(self, corpus_dir: Path, runs_dir: Path, sources_dir: Path | None = None):
        self.corpus_dir = Path(corpus_dir)
        self.runs_dir = Path(runs_dir)
        self.sources_dir = Path(sources_dir) if sources_dir else None

    def corpus_run_dir(self, run_id: str) -> Path:
        return self.corpus_dir / run_id

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def run_config(self, run_id: str) -> dict:
        return read_run(self.run_dir(run_id))

    def layer(self, source_id: str) -> str | None:
        """A source's rendered text layer, or None when this host holds no rendering."""
        if self.sources_dir is None:
            return None
        path = self.sources_dir / source_id / "layer.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None


def read_run(run_dir: Path) -> dict:
    """A run's configuration: ``run.yaml`` where PyYAML is importable, else ``run.json``.

    The run file is the planner's (stage 10). Only a handful of its keys are read here —
    the ``locate`` block's mode, status rule, sourceset, universe, inventory run and
    planned passage count — so a JSON spelling of the same block is accepted, which is
    what lets this stage be tested without a YAML parser.
    """
    run_dir = Path(run_dir)
    as_json = run_dir / "run.json"
    if as_json.is_file():
        data = json.loads(as_json.read_text(encoding="utf-8"))
    else:
        path = run_dir / "run.yaml"
        if not path.is_file():
            raise TallyProblem(f"no run.yaml or run.json in {run_dir}")
        try:
            import yaml  # noqa: PLC0415 — optional, and only for the planner's spelling
        except ImportError:  # pragma: no cover - PyYAML is pinned in requirements-ci.txt
            raise TallyProblem(f"{path} is YAML and this interpreter has no PyYAML; write the "
                               "run's locate block to run.json beside it") from None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("locate"), dict):
        raise TallyProblem(f"{run_dir}: not a locate run (its run file has no locate block)")
    return data


def status_rule_of(run: dict) -> int:
    """The status rule a run's outcomes are derived under: ``locate.status_rule``.

    A run file without the key is status rule 1, which is what every run planned before
    the statement cut was; one naming a rule this pipeline does not define is refused,
    never derived by another.
    """
    value = (run.get("locate") or {}).get("status_rule")
    if value is None:
        return vocab.STATUS_RULE_D1
    problem = vocab.status_rule_problem(value)
    if problem:
        raise TallyProblem(f"the run file names {problem}; tally it with the checkout that "
                           "planned it")
    return int(value)


class Universe:
    """The run's frozen universe, in the ``universe.json`` shape stage 6 writes."""

    def __init__(self, data: dict):
        self.id = str(data.get("id") or "")
        self.sha256 = str(data.get("universe_sha256") or "")
        rows = data.get("libraries") or []
        if isinstance(rows, dict):
            rows = [{"id": lid, **(lib or {})} for lid, lib in sorted(rows.items())]
        self.libraries = {str(lib.get("id")): dict(lib) for lib in rows}
        if not self.id or not vocab.HEX64.match(self.sha256) or not self.libraries:
            raise TallyProblem("a universe needs an id, a 64-hex universe_sha256 and at least "
                               "one library")
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "Universe":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def commit(self, library_id: str) -> str:
        return str(self.libraries[library_id]["commit"])


def read_manifest(path: Path | None) -> dict[str, tuple]:
    """``{source_id: (html_sha256, layer_sha256)}`` of the sourceset manifest a run pinned.

    A document whose pins are not the manifest's puts a problem on each of its rows; the
    export refuses those. Without a manifest nothing is pinned and the check is skipped,
    which is honest rather than silent: the report says the manifest was not read.
    """
    if path is None:
        return {}
    out: dict[str, tuple] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            sid = entry.get("source_id") or entry.get("id")
            if sid and (entry.get("html_sha256") or entry.get("layer_sha256")):
                out[str(sid)] = (entry.get("html_sha256"), entry.get("layer_sha256"))
    return out


# ---- small shared pieces ----------------------------------------------------------------


def decl_token(library: str, declaration: str, commit: str) -> str:
    """``<library>:<declaration>@<commit>``: a location's entry in the finding's decl list."""
    return f"{library}:{declaration}@{commit}"


def record_key(target: str, source_id: str, item: str, location_key: str | None = None) -> str:
    """The key a challenge plan and its verdicts agree on.

    A location is its ``location_key``; any other target — the absence, the unresolved
    outcome, the scope, the prominence, the coverage claim — is the run-free hash of the
    target, the source and the cleaned item.
    """
    if target == "location":
        if not location_key:
            raise TallyProblem("a location record needs its location_key")
        return location_key
    return keys.sha256_text("\n".join([target, source_id, items.clean(item)]))


def _problem(row: dict, problems: list[str], text: str) -> None:
    """A problem of one passage: listed, and put on its row so the export refuses it."""
    problems.append(text)
    row["problem"] = "; ".join(filter(None, [row.get("problem"), text]))


def _stamp(doc: dict) -> dict:
    return {"source_sha": doc.get("source_sha"), "layer_sha256": doc.get("layer_sha256")}


def _unresolved_by(doc: dict) -> str:
    """Who left a passage unresolved: the attempt cap's document says so, else the worker."""
    return str((doc.get("unresolved") or {}).get("by") or vocab.UNRESOLVED_BY_WORKER)


def _counts_toward_status(loc: dict) -> bool:
    """Whether a location counts: a ``relatedMatch`` never does."""
    return loc.get("relation") != vocab.RELATED_MATCH


# ---- reading a run ----------------------------------------------------------------------


def _documents(workspace: Workspace, run_id: str, *, require_gate: bool
               ) -> tuple[list[tuple[str, dict]], dict]:
    run_dir = workspace.corpus_run_dir(run_id)
    gate = corpus_mod.gate_summary(run_dir, run_id)
    if require_gate:
        corpus_mod.require_gated(run_dir, run_id, "run")
    return corpus_mod.documents(run_dir), gate


def inventory(workspace: Workspace, run_id: str, *, require_gate: bool = True) -> dict:
    """The frozen passage table of an inventory run: ``{(source_id, join key): passage}``.

    Two documents for one source, or one passage twice, are refused: a passage table is
    frozen once. A run that was never harvested is refused, never read as an empty table.
    """
    if corpus_mod.receipt(workspace.corpus_run_dir(run_id)) is None:
        raise TallyProblem(f"inventory run {run_id!r} has no RECEIPT.json under "
                           f"{workspace.corpus_run_dir(run_id)}")
    docs, gate = _documents(workspace, run_id, require_gate=require_gate)
    passages: dict[tuple[str, str], dict] = {}
    unresolved: dict[str, dict] = {}
    seen_sources: dict[str, str] = {}
    for rel, doc in docs:
        if doc.get("mode") != vocab.MODE_INVENTORY:
            raise TallyProblem(f"{rel} in inventory run {run_id} is a {doc.get('mode')!r} "
                               "document")
        sid = corpus_mod.source_of(rel, doc)
        if sid in seen_sources:
            raise TallyProblem(f"inventory run {run_id} holds two documents for {sid}: "
                               f"{seen_sources[sid]} and {rel}")
        seen_sources[sid] = rel
        if isinstance(doc.get("unresolved"), dict):
            unresolved[sid] = dict(doc["unresolved"])
            continue
        for row in doc.get("passages") or []:
            item = str(row.get("item") or "")
            key = (sid, items.join_key(item))
            if key in passages:
                raise TallyProblem(f"{rel}: passage {item} is listed twice")
            passages[key] = {**row, "source_id": sid, "item": item, **_stamp(doc),
                             "task_id": corpus_mod.task_of(rel)}
    return {"passages": passages, "unresolved": unresolved, "documents": len(docs),
            "gate": gate}


def task_manifests(workspace: Workspace, run_id: str) -> list[dict]:
    """The run's task manifests, as planned: ``runs/<run>/planned.json``.

    Else the staged gate's ``gate/tasks/*.json``, which is what a run host holds when a
    workspace did not keep the planner's own file; [] when neither is there.
    """
    planned = workspace.run_dir(run_id) / "planned.json"
    if planned.is_file():
        data = json.loads(planned.read_text(encoding="utf-8"))
        return list(data.get("tasks") or [])
    directory = workspace.run_dir(run_id) / "tend-root" / "gate" / "tasks"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))]


def _planned(workspace: Workspace, run_id: str, loc_block: dict) -> dict:
    """``{(source_id, join key): planned passage}``, refused when it is not what was planned."""
    planned: dict[tuple[str, str], dict] = {}
    for manifest in task_manifests(workspace, run_id):
        for doc in manifest.get("documents") or []:
            for passage in doc.get("passages") or []:
                planned[(doc["source_id"], items.join_key(passage["item"]))] = {
                    **passage, "task_id": manifest.get("task_id")}
    want = loc_block.get("passages")
    if want is not None and int(want) != len(planned):
        raise TallyProblem(f"run {run_id} planned {want} passage(s) and its task manifests hold "
                           f"{len(planned)}: restore planned.json, or re-plan the run")
    return planned


# ---- finding rows -----------------------------------------------------------------------


def _row(run_id: str, source_id: str, item: str, kind: str, repo: str,
         decls: list[str], **extra) -> dict:
    return {"finding_id": keys.finding_id(run_id, source_id, item, kind, decls),
            "run_id": run_id, "source_id": source_id, "item": item, "kind": kind,
            "repo": repo, "unfaithful_decls": decls, **extra}


def finding_rows(run_id: str, docs: Iterable[tuple[str, dict]], *, sourceset_id: str,
                 universe: Universe | None,
                 accepted: set[tuple[str, str]] | None = None) -> list[dict]:
    """Every document's finding rows, in document order.

    ``accepted`` — ``{(document path, item)}`` — keeps a locate document's rows to the
    passages the outcomes took from it, so a passage answered twice yields one set.
    """
    rows: list[dict] = []
    for rel, doc in docs:
        mode = doc.get("mode")
        sid = corpus_mod.source_of(rel, doc)
        stamp = _stamp(doc)
        if mode == vocab.MODE_INVENTORY:
            if isinstance(doc.get("unresolved"), dict):
                rows.append(_row(run_id, sid, "", vocab.ROW_UNRESOLVED, sourceset_id, [],
                                 by=doc["unresolved"].get("by") or vocab.UNRESOLVED_BY_WORKER,
                                 **stamp))
                continue
            for passage in doc.get("passages") or []:
                rows.append(_row(run_id, sid, str(passage.get("item") or ""), vocab.ROW_PASSAGE,
                                 sourceset_id, [], passage_kind=passage.get("passage_kind"),
                                 scope=passage.get("scope"), **stamp))
            continue
        if mode != vocab.MODE_LOCATE:
            continue
        if universe is None:
            raise TallyProblem("a locate document's rows need the run's universe")
        for passage in doc.get("passages") or []:
            item = str(passage.get("item") or "")
            if accepted is not None and (rel, item) not in accepted:
                continue
            outcome = passage.get("outcome")
            for loc in passage.get("locations") or []:
                lib, name = str(loc.get("library")), str(loc.get("declaration"))
                commit = universe.libraries.get(lib, {}).get("commit", "")
                rows.append(_row(run_id, sid, item, vocab.ROW_LOCATION, lib,
                                 [decl_token(lib, name, commit)],
                                 relation=loc.get("relation"), **stamp))
            if outcome == vocab.OUTCOME_ABSENT:
                rows.append(_row(run_id, sid, item, vocab.ROW_ABSENCE, universe.id, [], **stamp))
            elif outcome == vocab.OUTCOME_UNRESOLVED:
                rows.append(_row(run_id, sid, item, vocab.ROW_UNRESOLVED, universe.id, [],
                                 by=_unresolved_by(doc), **stamp))
    return rows


# ---- the challenge ----------------------------------------------------------------------


def challenge_values(workspace: Workspace, run_id: str, challenge_runs: Iterable[str], *,
                     require_gate: bool = True, conditional: dict | None = None,
                     fills: dict | None = None) -> tuple[dict, dict, list[str], dict]:
    """``(planned, verdicts, problems, gates)`` of the challenge runs of locate run ``run_id``.

    ``planned`` is ``{record key: plan row}`` — the records of each run's
    ``challenge-plan.json`` that are ``run_id``'s; ``verdicts`` is ``{record key: verdict}``
    from its harvested documents, kept only for a record the plan REQUIRED (a verdict on
    any other is named in ``problems`` and ignored); ``gates`` is each challenge run's
    receipt summary. A challenge run that did not challenge ``run_id`` is refused; one
    whose plan requires nothing has no documents to read. One record planned or judged
    twice is refused.

    Two optional dicts the caller passes are filled, so the four-value result stays what
    every reader takes: ``conditional`` gets the verdicts on records the plan marked
    ``conditional: "prominence"``, which apply only beside an overturn of that passage's
    prominence in the same run; ``fills`` gets each document's ``fills``, what replaces
    the last counting location a verdict removed.
    """
    planned: dict[str, dict] = {}
    verdicts: dict[str, dict] = {}
    problems: list[str] = []
    gates: dict[str, dict] = {}
    for challenge_run in challenge_runs:
        plan_path = workspace.run_dir(challenge_run) / "challenge-plan.json"
        if not plan_path.is_file():
            raise TallyProblem(f"challenge run {challenge_run!r} has no challenge-plan.json")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("schema") != vocab.CHALLENGE_PLAN_SCHEMA:
            raise TallyProblem(f"{plan_path} is not a {vocab.CHALLENGE_PLAN_SCHEMA} document")
        if run_id not in (plan.get("challenged_runs") or []):
            raise TallyProblem(f"challenge run {challenge_run!r} challenged "
                               f"{', '.join(plan.get('challenged_runs') or []) or 'nothing'}, "
                               f"not {run_id}")
        required = 0
        for rec in plan.get("records") or []:
            if rec.get("run_id") != run_id:
                continue
            key = rec["key"]
            if key in planned:
                raise TallyProblem(f"record {key[:12]} is planned by two challenge runs")
            planned[key] = {**rec, "challenge_run": challenge_run}
            required += rec.get("planned") == vocab.CHALLENGE_REQUIRED
        if not any(rec.get("planned") == vocab.CHALLENGE_REQUIRED
                   for rec in plan.get("records") or []):
            gates[challenge_run] = {"receipt": None, "complete": None, "gate_ok": None,
                                    "required": 0}
            continue
        docs, gate = _documents(workspace, challenge_run, require_gate=require_gate)
        gates[challenge_run] = {**gate, "required": required}
        for rel, doc in docs:
            if doc.get("mode") != vocab.MODE_CHALLENGE:
                problems.append(f"{rel} in challenge run {challenge_run} is not a challenge "
                                "document")
                continue
            sid = corpus_mod.source_of(rel, doc)
            for verdict in doc.get("verdicts") or []:
                target = str(verdict.get("target") or "")
                try:
                    key = record_key(target, sid, str(verdict.get("item") or ""),
                                     verdict.get("location_key"))
                except TallyProblem as exc:
                    problems.append(f"{rel}: {exc}")
                    continue
                plan_row = planned.get(key)
                if plan_row is None or plan_row["challenge_run"] != challenge_run:
                    problems.append(f"{rel}: a verdict on {target} {verdict.get('item')} is not "
                                    f"a record {challenge_run} planned for {run_id}; ignored")
                    continue
                if plan_row.get("planned") != vocab.CHALLENGE_REQUIRED:
                    if conditional is not None and \
                            plan_row.get("conditional") == CONDITIONAL_PROMINENCE:
                        # judged in the task of its passage's prominence record; it counts
                        # only beside that prominence's overturn (_apply_challenge)
                        if key in conditional:
                            raise TallyProblem(f"{rel}: record {key[:12]} is judged twice")
                        conditional[key] = {**verdict, "source_id": sid,
                                            "challenge_run": challenge_run}
                        continue
                    problems.append(f"{rel}: {target} {verdict.get('item')} was planned "
                                    f"{plan_row.get('planned')}; the verdict is ignored")
                    continue
                if key in verdicts:
                    raise TallyProblem(f"{rel}: record {key[:12]} is judged twice")
                verdicts[key] = {**verdict, "source_id": sid, "challenge_run": challenge_run}
                claims = doc.get("claims")
                if isinstance(claims, dict) and isinstance(claims.get(verdict.get("item")), list):
                    # the challenger's claim list of the passage, which its clauses name
                    verdicts[key]["claims"] = claims[verdict.get("item")]
            if fills is not None and isinstance(doc.get("fills"), dict):
                for item, fill in sorted(doc["fills"].items()):
                    pkey = (sid, items.join_key(str(item)))
                    if pkey in fills:
                        problems.append(f"{rel}: {sid} {item} is filled twice; the second fill "
                                        "is ignored")
                        continue
                    fills[pkey] = {**(fill if isinstance(fill, dict) else {"outcome": fill}),
                                   "item": str(item), "source_id": sid,
                                   "challenge_run": challenge_run, "document": rel}
    return planned, verdicts, problems, gates


def _challenge_value(key: str, planned: dict, verdicts: dict) -> str | None:
    if key in verdicts:
        action = verdicts[key].get("action")
        return (vocab.CHALLENGE_SURVIVED if action in vocab.CHALLENGE_KEEP
                else vocab.CHALLENGE_DOWNGRADED)
    plan = planned.get(key)
    if plan is None:
        return None
    if plan.get("planned") == vocab.CHALLENGE_REQUIRED:
        return None
    return plan.get("planned")


def passage_challenge(row: dict) -> str | None:
    """One challenge value for a passage over all of its records.

    Its locations' (the refuted ones included: a refutation is a judged record), its
    absence's or unresolved outcome's, its coverage claim's (a refuted one included), and
    its prominence record's when a plan named one: owed (``None``) when any of them is
    owed, else ``challenge_pending`` when a rule now requires one nobody judged, else
    ``downgraded`` when any changed, else ``survived`` when any was judged.
    """
    values = [loc.get("challenge") for loc in [*row["locations"], *(row.get("refuted") or [])]]
    for claim in (row.get("coverage"), row.get("coverage_refuted")):
        if claim is not None:
            values.append(claim.get("challenge"))
    if row.get("record_challenge", "unset") != "unset":
        values.append(row.get("record_challenge"))
    if row.get("prominence_challenge", "unset") != "unset":
        values.append(row.get("prominence_challenge"))
    if not values or any(value is None for value in values):
        return None
    if vocab.CHALLENGE_PENDING in values:
        return vocab.CHALLENGE_PENDING
    if row.get("passage_challenge_override"):
        return row["passage_challenge_override"]
    if vocab.CHALLENGE_DOWNGRADED in values:
        return vocab.CHALLENGE_DOWNGRADED
    if vocab.CHALLENGE_SURVIVED in values:
        return vocab.CHALLENGE_SURVIVED
    if vocab.CHALLENGE_SAMPLED_OUT in values:
        return vocab.CHALLENGE_SAMPLED_OUT
    return vocab.CHALLENGE_NOT_REQUIRED


# ---- one location ------------------------------------------------------------------------


def _location_fields(status_rule: int) -> tuple[str, ...]:
    """The judgment fields a location of a run of ``status_rule`` keeps, where written."""
    if vocab.reads_bundles(status_rule):
        return vocab.JUDGMENT_LOCATION_FIELDS
    return tuple(key for key in vocab.JUDGMENT_LOCATION_FIELDS
                 if key not in CUT_LOCATION_FIELDS)


def _location(source_id: str, item: str, loc: dict, universe: Universe,
              declarations: index_mod.DeclarationIndex | None, problems: list[str], *,
              status_rule: int = vocab.STATUS_RULE_D1) -> dict | None:
    lib = str(loc.get("library") or "")
    name = str(loc.get("declaration") or "")
    if lib not in universe.libraries:
        problems.append(f"{source_id} {item}: library {lib!r} is not in universe {universe.id}")
        return None
    commit = universe.commit(lib)
    out = {"library": lib, "commit": commit, "declaration": name,
           "relation": loc.get("relation"), "rank": loc.get("rank"),
           "confidence": loc.get("confidence"), "note": loc.get("note") or "",
           "location_key": keys.location_key(source_id, item, lib, commit, name)}
    # what the judgment rules have a location say of its passage, kept as written and only
    # where written; judgment rules 3's two only on a run that reads bundles
    out.update({key: loc[key] for key in _location_fields(status_rule) if key in loc})
    if vocab.reads_bundles(status_rule):
        # a field the relation does not take is dropped here as after a verdict, so a row's
        # location is the same whether or not a challenge judged it
        _fit_fields(out)
    if "proof_status" in loc:
        problems.append(f"{source_id} {item}: a document wrote proof_status for {lib}:{name}; "
                        "it is joined from the index, and the written value was ignored")
    if declarations is not None:
        row = declarations.declaration(lib, name, commit)
        if row is None:
            problems.append(f"{source_id} {item}: {lib}:{name} is not in the declaration index "
                            f"at {commit[:12]}")
            return None
        out.update({"proof_status": row["proof_status"], "statement": row["statement"],
                    "module": row.get("module"), "path": row.get("path"),
                    "line": row.get("line"), "end_line": row.get("end_line"),
                    "kind": row.get("kind")})
    return out


def _join_locations(sid: str, item: str, given: Iterable[dict], universe: Universe,
                    declarations, problems: list[str], *,
                    status_rule: int = vocab.STATUS_RULE_D1) -> tuple[list[dict], list[str]]:
    """``(joined locations by rank, why each dropped one was dropped)``.

    A declaration listed twice keeps its best-ranked copy (and says so); a location that
    cannot be joined — a library outside the universe, a declaration missing from the
    index — is dropped, and its reason returned so the caller can put it on the row.
    """
    dropped: list[str] = []
    joined: dict[str, dict] = {}
    for loc in given:
        before = len(problems)
        got = _location(sid, item, loc, universe, declarations, problems,
                        status_rule=status_rule)
        if got is None:
            dropped.extend(problems[before:])
            continue
        key = got["location_key"]
        if key in joined:
            problems.append(f"{sid} {item}: {got['library']}:{got['declaration']} is listed "
                            "twice; the best-ranked copy is kept")
            if (got.get("rank") or 99) >= (joined[key].get("rank") or 99):
                continue
        joined[key] = got
    ordered = sorted(joined.values(),
                     key=lambda loc: (loc.get("rank") or 99, loc["library"], loc["declaration"]))
    return ordered, dropped


# ---- the outcome rows ---------------------------------------------------------------------


def _base_fields(sid: str, item: str, base: dict) -> dict:
    return {"source_id": sid, "item": item,
            "passage_kind": base.get("passage_kind"), "prominence": base.get("prominence"),
            "scope": base.get("scope") or vocab.SCOPE_FORMAL_MATH,
            "span": list(base["span"]) if base.get("span") else None,
            "head_words": base.get("head_words"), "section": base.get("section"),
            "quote": base.get("quote"), "source_sha": base.get("source_sha"),
            "layer_sha256": base.get("layer_sha256")}


def _statement_fields(base: dict, status_rule: int) -> dict:
    """What a row of status rule 2 or 3 copies from its frozen inventory passage."""
    if not vocab.reads_bundles(status_rule):
        return {}
    return {key: base[key] for key in STATEMENT_FIELDS if base.get(key) is not None}


def coverage_claim(given, outcome, locations: list[dict]) -> tuple[dict | None, str]:
    """A locate document's coverage claim, its parts turned from ranks into location keys.

    ``(claim, "")`` — ``{complete: True, parts: [location_key, ...], note, challenge: None}``,
    the parts in the order the document names them — or ``(None, why)`` when the claim is
    not one a record set can carry: not on a located passage, not ``complete: true``,
    fewer than two distinct parts, a part that is the rank of no joined ``hasPart``
    location, or no note. The gate refuses each of these first.
    """
    if outcome != vocab.OUTCOME_LOCATED:
        return None, f"a coverage claim on an {outcome} passage is not one"
    if not isinstance(given, dict) or given.get("complete") is not True:
        return None, "a coverage claim says complete: true"
    ranks = given.get("parts")
    if not isinstance(ranks, list) or not all(isinstance(rank, int) and not isinstance(rank, bool)
                                              for rank in ranks) \
            or len(set(ranks)) != len(ranks) or len(ranks) < vocab.MIN_COVERAGE_PARTS:
        return None, (f"a coverage claim names at least {vocab.MIN_COVERAGE_PARTS} distinct "
                      f"ranks, not {ranks!r}")
    by_rank = {loc.get("rank"): loc for loc in locations}
    part_keys = []
    for rank in ranks:
        loc = by_rank.get(rank)
        if loc is None or loc.get("relation") != vocab.HAS_PART:
            return None, (f"coverage part {rank} is not the rank of one of the passage's "
                          "joined hasPart locations")
        part_keys.append(loc["location_key"])
    note = " ".join(str(given.get("note") or "").split())
    if not note:
        return None, "a coverage claim says in its note how the parts state every clause"
    return {"complete": True, "parts": part_keys, "note": note, "challenge": None}, ""


def _outcome(run_id: str, sid: str, item: str, passage: dict, doc: dict, base: dict,
             universe: Universe, declarations, problems: list[str], *, task_id: str,
             inv_run: str, status_rule: int = vocab.STATUS_RULE_D1) -> dict:
    outcome = passage.get("outcome")
    own: list[str] = []
    if outcome not in vocab.LOCATE_OUTCOMES:
        own.append(f"{sid} {item}: outcome {outcome!r} is not one of "
                   f"{', '.join(sorted(vocab.LOCATE_OUTCOMES))}")
        outcome = vocab.OUTCOME_UNRESOLVED
    locations, dropped = _join_locations(sid, item, passage.get("locations") or [], universe,
                                         declarations, problems, status_rule=status_rule)
    if outcome == vocab.OUTCOME_UNRESOLVED and locations:
        own.append(f"{sid} {item}: an unresolved passage has no locations "
                   f"({len(locations)} given)")
    receipt = passage.get("receipt") if isinstance(passage.get("receipt"), dict) else {}
    coverage = None
    if passage.get("coverage") is not None:
        coverage, why = coverage_claim(passage["coverage"], outcome, locations)
        if why:
            own.append(f"{sid} {item}: {why}")
    row = {**_base_fields(sid, item, base), **_stamp(doc), "outcome": outcome,
           "locations": locations, "coverage": coverage, "coverage_refuted": None,
           "absence": ({"receipt": receipt, "challenge": None}
                       if outcome == vocab.OUTCOME_ABSENT else None),
           "receipt": receipt, "reason": passage.get("reason") or "",
           "unresolved_by": _unresolved_by(doc) if outcome == vocab.OUTCOME_UNRESOLVED else None,
           "missing": False, "refuted": [], "challenge": None, "status": None,
           "runs": {"inventory": inv_run or None, "locate": run_id, "challenge": None},
           "task_id": task_id}
    if isinstance(passage.get("claims"), list):
        # the passage's claim list (the judgment rules): what its locations' clauses name
        row["claims"] = passage["claims"]
    row.update(_statement_fields(base, status_rule))
    if vocab.reads_bundles(status_rule) and outcome == vocab.OUTCOME_UNRESOLVED \
            and passage.get("cut_flag") is not None:
        # what a worker who cannot re-cut the passage says of its cut, counted for the
        # inventory audit
        if passage["cut_flag"] in vocab.CUT_FLAGS:
            row["cut_flag"] = passage["cut_flag"]
        else:
            own.append(f"{sid} {item}: cut_flag {passage['cut_flag']!r} is not one of "
                       f"{', '.join(sorted(vocab.CUT_FLAGS))}")
    for text in own:
        _problem(row, problems, text)
    for text in dropped:
        row["problem"] = "; ".join(filter(None, [row.get("problem"), text]))
    return row


def _missing(run_id: str, sid: str, item: str, base: dict, plan: dict, *,
             status_rule: int = vocab.STATUS_RULE_D1) -> dict:
    return {**_base_fields(sid, item, base), **_statement_fields(base, status_rule),
            "outcome": None, "locations": [], "coverage": None, "coverage_refuted": None,
            "absence": None, "receipt": {}, "reason": "no document landed for this passage",
            "unresolved_by": None, "missing": True, "refuted": [], "challenge": None,
            "status": None, "runs": {"inventory": None, "locate": run_id, "challenge": None},
            "task_id": plan.get("task_id")}


def _scoped_out(run_id: str, sid: str, item: str, base: dict, inv_run: str, *,
                status_rule: int = vocab.STATUS_RULE_D1) -> dict:
    return {**_base_fields(sid, item, base), **_statement_fields(base, status_rule),
            "outcome": None, "locations": [], "coverage": None, "coverage_refuted": None,
            "absence": None, "receipt": {}, "reason": f"scoped {base.get('scope')}",
            "unresolved_by": None, "missing": False, "refuted": [], "challenge": None,
            "status": None, "runs": {"inventory": inv_run, "locate": run_id, "challenge": None},
            "task_id": None}


# ---- applying the challenge ----------------------------------------------------------------


def _conditional_verdicts(row: dict, planned: dict, verdicts: dict,
                          conditional: dict | None, problems: list[str]) -> dict:
    """``verdicts`` with the passage's conditional verdicts that apply.

    A record the plan marked ``conditional: "prominence"`` was put in the task of its
    passage's required prominence record with its planned value unchanged; its verdict
    applies only when the same challenge run overturned that prominence to ``subject`` or
    ``central``. Otherwise each is named and ignored, as a verdict on any record the plan
    did not require is.
    """
    if not conditional:
        return verdicts
    sid, item = row["source_id"], row["item"]
    pkey = record_key("prominence", sid, item)
    overturn = verdicts.get(pkey)
    plan = planned.get(pkey) or {}
    run = None
    if overturn is not None and overturn.get("action") == "overturn" \
            and overturn.get("prominence") in vocab.REFUTE_BY_DEFAULT_PROMINENCE \
            and overturn.get("prominence") != row.get("prominence") \
            and plan.get("outcome") == row.get("outcome"):
        run = overturn.get("challenge_run")
    out = dict(verdicts)
    for key, verdict in sorted(conditional.items()):
        if run is not None and verdict.get("challenge_run") == run:
            out[key] = verdict
        else:
            problems.append(f"{sid} {item}: a verdict on the conditional "
                            f"{verdict.get('target')} record {key[:12]} with no overturn of the "
                            "passage's prominence to subject or central in its challenge run; "
                            "ignored")
    return out


def _apply_challenge(row: dict, planned: dict, verdicts: dict, universe, declarations,
                     problems: list[str], layer_of=None, *,
                     status_rule: int = vocab.STATUS_RULE_D1, conditional: dict | None = None,
                     fill: dict | None = None) -> None:
    sid, item = row["source_id"], row["item"]
    outcome_seen = row["outcome"]
    verdicts = _conditional_verdicts(row, planned, verdicts, conditional, problems)
    # whether the passage counted before the challenge: one it leaves without is owed a hunt
    row["counted_before"] = any(_counts_toward_status(loc) for loc in row["locations"])
    # locations first: their own records
    kept = []
    for loc in row["locations"]:
        key = loc["location_key"]
        plan = planned.get(key)
        if plan is not None and plan.get("relation") != loc["relation"]:
            loc["challenge"] = None
            _problem(row, problems, f"{sid} {item}: {loc['library']}:{loc['declaration']} is "
                                    f"{loc['relation']} and the challenge planned it as "
                                    f"{plan.get('relation')}; it is owed again")
            kept.append(loc)
            continue
        verdict = verdicts.get(key)
        loc["challenge"] = _challenge_value(key, planned, verdicts)
        if verdict is not None:
            row["runs"]["challenge"] = verdict["challenge_run"]
            action = verdict.get("action")
            if isinstance(verdict.get("claims"), list):
                row["challenge_claims"] = verdict["claims"]
            if action in vocab.CHALLENGE_KEEP:
                _judged_fields(loc, verdict, row, problems, status_rule=status_rule)
            if action == "refute":
                row["refuted"].append({**loc, "witness": verdict.get("witness", "")})
                row["passage_challenge_override"] = vocab.CHALLENGE_DOWNGRADED
                continue
            if action == "downgrade":
                relation = verdict.get("relation")
                if relation not in vocab.LOCATION_RELATIONS or \
                        RELATION_STRENGTH[relation] <= RELATION_STRENGTH[loc["relation"]]:
                    loc["challenge"] = None
                    _problem(row, problems, f"{sid} {item}: a downgrade of "
                                            f"{loc['library']}:{loc['declaration']} from "
                                            f"{loc['relation']} to {relation!r} is not weaker; "
                                            "it is ignored and the record owed")
                else:
                    loc["relation"] = relation
                    if relation == vocab.CLOSE_MATCH and not str(loc.get("note") or "").strip():
                        loc["note"] = records_mod.downgrade_note(
                            verdict, layer_of(sid) if layer_of else None)
                    _judged_fields(loc, verdict, row, problems, status_rule=status_rule)
        kept.append(loc)
    row["locations"] = _renumbered(kept, status_rule=status_rule)
    confirmed: list[dict] = []
    if row.get("coverage") is not None:
        confirmed = _apply_coverage(row, planned, verdicts, problems, status_rule=status_rule)
    # the passage-level record: an absence, an unresolved outcome, a scope or a prominence
    target = {vocab.OUTCOME_ABSENT: "absence",
              vocab.OUTCOME_UNRESOLVED: "unresolved"}.get(row["outcome"])
    if target is None and row["outcome"] is None and not row["missing"]:
        target = "scope"
    for name in [t for t in (target, "prominence") if t]:
        key = record_key(name, sid, item)
        plan = planned.get(key)
        if plan is not None and plan.get("outcome") != outcome_seen:
            _problem(row, problems, f"{sid} {item}: the {name} record was planned for outcome "
                                    f"{plan.get('outcome')!r} and the passage is "
                                    f"{outcome_seen!r}; it is owed again")
            if name == "prominence":
                row["prominence_challenge"] = None
            else:
                row["record_challenge"] = None
                if row["absence"] is not None:
                    row["absence"]["challenge"] = None
            continue
        value = _challenge_value(key, planned, verdicts)
        if name in ("absence", "unresolved", "scope"):
            row["record_challenge"] = value
            if row["absence"] is not None:
                row["absence"]["challenge"] = value
        elif key in planned or key in verdicts:
            row["prominence_challenge"] = value
        verdict = verdicts.get(key)
        if verdict is None:
            continue
        row["runs"]["challenge"] = verdict["challenge_run"]
        action = verdict.get("action")
        if isinstance(verdict.get("claims"), list) and name != "prominence":
            row["challenge_claims"] = verdict["claims"]
        if action == "uphold" and name in ("absence", "unresolved") \
                and isinstance(verdict.get("queries"), list):
            # what the challenger searched before it upheld the record: its queries,
            # tagged with its own claims
            row["challenge_receipt"] = {"queries": verdict["queries"],
                                        "query_clauses": verdict.get("query_clauses")}
            if vocab.reads_bundles(status_rule) and isinstance(verdict.get("special_case"), dict):
                # the nearest special case the challenger found and why it fails
                row["challenge_receipt"]["special_case"] = verdict["special_case"]
        if action == "overturn" and name in ("absence", "unresolved", "scope"):
            new, dropped = _join_locations(sid, item, verdict.get("locations") or [], universe,
                                           declarations, problems, status_rule=status_rule)
            for text in dropped:
                row["problem"] = "; ".join(filter(None, [row.get("problem"), text]))
            # the overturned record is gone, and each location the challenger added is its
            # own witnessed, gated finding — challenged and standing, never a weakened one
            for loc in new:
                loc["challenge"] = vocab.CHALLENGE_SURVIVED
            row["record_challenge"] = vocab.CHALLENGE_DOWNGRADED
            row["passage_challenge_override"] = vocab.CHALLENGE_DOWNGRADED
            if name == "scope":
                row["scope"] = vocab.SCOPE_FORMAL_MATH
                if not new:
                    _problem(row, problems, f"{sid} {item}: scope overturned to formal_math "
                                            "with no location; the passage needs a locate pass")
                    continue
            elif not new:
                _problem(row, problems, f"{sid} {item}: an overturn with no location that exists")
                continue
            row["locations"] = _overturned(new, row["locations"])
            if isinstance(verdict.get("claims"), list):
                # the overturn's locations name the challenger's claim list
                row["claims"] = verdict["claims"]
            row["outcome"] = vocab.OUTCOME_LOCATED
            row["scope"] = vocab.SCOPE_FORMAL_MATH
            row["absence"] = None
            row["unresolved_by"] = None
        elif name == "prominence" and action == "overturn":
            if verdict.get("prominence") == row["prominence"]:
                # an overturn to the passage's own prominence overturns nothing
                row["prominence_challenge"] = None
                _problem(row, problems, f"{sid} {item}: a prominence overturn to "
                                        f"{row['prominence']!r}, the passage's own, changes "
                                        "nothing; it is ignored and the record owed")
                continue
            if verdict.get("prominence") in vocab.PROMINENCES:
                row["prominence"] = verdict["prominence"]
            row["passage_challenge_override"] = vocab.CHALLENGE_DOWNGRADED
            if row["prominence"] in vocab.REFUTE_BY_DEFAULT_PROMINENCE:
                _reopen(row)
    # a confirmed claim confirms its parts only now, after any reopen: the reopen wins, so a
    # part a prominence overturn put under a rule requiring it is challenge_pending, owed its
    # own verdict under that rule, and the claim does not stand in for it
    confirm_parts(confirmed)
    if fill is not None:
        if vocab.reads_bundles(status_rule):
            _apply_fill(row, fill, universe, declarations, problems)
        else:
            problems.append(f"{sid} {item}: a fill from {fill.get('challenge_run')} on a run of "
                            f"status rule {status_rule}, which no fill binds; ignored")


def _apply_fill(row: dict, fill: dict, universe, declarations, problems: list[str]) -> None:
    """A challenge document's ``fills[item]``: what replaces the last counting location.

    A document whose verdicts leave a ``located`` passage with no counting location gives
    ``{outcome: absent, receipt}`` — the challenger hunted and found nothing, so the
    passage is ``absent`` beside its ``relatedMatch`` locations, the absence the
    challenger's own — or ``{outcome: located, locations}`` — the replacements it found,
    each its own witnessed finding, first, then the kept ``relatedMatch`` ones, as an
    overturn's. A fill applies only when the passage counted before the challenge, its
    challenge run's verdicts left it with no counting location and no absence, and those
    verdicts are the fill's own run's; otherwise it is named on the row and ignored.
    """
    sid, item = row["source_id"], row["item"]
    run = fill.get("challenge_run")
    if row["outcome"] != vocab.OUTCOME_LOCATED or row.get("absence") is not None \
            or not row.get("counted_before") \
            or any(_counts_toward_status(loc) for loc in row["locations"]) \
            or row["runs"].get("challenge") != run:
        _problem(row, problems, f"{sid} {item}: {run}'s fill names a passage its verdicts did "
                                "not leave without a location that counts; it is ignored")
        return
    outcome = fill.get("outcome")
    if outcome == vocab.OUTCOME_ABSENT and isinstance(fill.get("receipt"), dict):
        row["outcome"] = vocab.OUTCOME_ABSENT
        row["absence"] = {"receipt": fill["receipt"], "challenge": vocab.CHALLENGE_SURVIVED}
        row["fill"] = {"outcome": outcome, "challenge_run": run}
        return
    if outcome == vocab.OUTCOME_LOCATED and isinstance(fill.get("locations"), list):
        new, dropped = _join_locations(sid, item, fill["locations"], universe, declarations,
                                       problems, status_rule=vocab.STATUS_RULE_BUNDLE)
        for text in dropped:
            row["problem"] = "; ".join(filter(None, [row.get("problem"), text]))
        new = [loc for loc in new if _counts_toward_status(loc)]
        if not new:
            _problem(row, problems, f"{sid} {item}: {run}'s fill names no location that exists "
                                    "and counts")
            return
        for loc in new:
            loc["challenge"] = vocab.CHALLENGE_SURVIVED
        row["locations"] = _overturned(new, row["locations"])
        row["fill"] = {"outcome": outcome, "challenge_run": run}
        return
    _problem(row, problems, f"{sid} {item}: a fill is {{outcome: absent, receipt}} or "
                            f"{{outcome: located, locations}}, not {fill.get('outcome')!r}; it "
                            "is ignored")


def _judged_fields(loc: dict, verdict: dict, row: dict | None = None,
                   problems: list[str] | None = None, *,
                   status_rule: int = vocab.STATUS_RULE_D1) -> None:
    """What a challenger says the location it leaves states (the judgment rules).

    Called for a confirm, and for a downgrade once it is applied. ``clauses`` name the
    challenger's own claim list of the passage, so the location keeps them as
    ``challenge_clauses`` with its worker's ``clauses`` unchanged; a ``step``,
    ``convention`` or ``instantiation`` the verdict names replaces the worker's. Then a
    field that does not describe the relation the location now has is dropped
    (:func:`_fit_fields`), so a downgrade from ``closeMatch`` to ``broadMatch`` leaves no
    convention behind.
    """
    if isinstance(verdict.get("clauses"), list):
        loc["challenge_clauses"] = verdict["clauses"]
    for key in VERDICT_FIELDS:
        if isinstance(verdict.get(key), str) and verdict[key].strip():
            loc[key] = verdict[key]
    if vocab.reads_bundles(status_rule):
        _judged_cut_fields(loc, verdict, row, problems)
    _fit_fields(loc)


def _judged_cut_fields(loc: dict, verdict: dict, row: dict | None,
                       problems: list[str] | None) -> None:
    """A verdict's ``tests`` and ``conjunct_relation`` on the location it leaves.

    And a downgrade to ``relatedMatch``'s ``bridge`` or ``incomparable``, kept as the
    verdict gave it. A ``conjunct_relation`` is never stronger than the location's own: a
    challenge never raises a relation, so a stronger one is named and the location's kept.
    """
    if isinstance(verdict.get("tests"), dict):
        loc["tests"] = verdict["tests"]
    for key in vocab.BRIDGE_FIELDS:
        if verdict.get(key) is not None:
            loc[key] = verdict[key]
    given = verdict.get("conjunct_relation")
    if given is None:
        return
    own = loc.get("conjunct_relation")
    why = None
    if given not in vocab.CONJUNCT_RELATIONS_PART:
        why = (f"{given!r}, which is none of "
               f"{', '.join(sorted(vocab.CONJUNCT_RELATIONS_PART))}")
    elif own in RELATION_STRENGTH and RELATION_STRENGTH[given] < RELATION_STRENGTH[own]:
        why = f"{given!r}, stronger than its own {own!r}"
    if why is None:
        loc["conjunct_relation"] = given
    elif row is not None and problems is not None:
        _problem(row, problems, f"{row['source_id']} {row['item']}: a verdict gives "
                                f"{loc.get('library')}:{loc.get('declaration')} the conjunct "
                                f"relation {why}; the location's is kept")


def held_relation(loc: dict) -> str | None:
    """The relation a location is held to: its own, or a ``hasPart``'s ``conjunct_relation``.

    Judgment rules 3 hold a part of a compound statement by the relation it bears to the
    conjuncts it states — the relation a bundle takes from it — so a part whose conjunct
    relation is strong is a strong location, with a strong location's ``tests``.
    """
    relation = loc.get("relation")
    if relation == vocab.HAS_PART and \
            loc.get("conjunct_relation") in vocab.CONJUNCT_RELATIONS_PART:
        return loc["conjunct_relation"]
    return relation


def _fit_fields(loc: dict) -> None:
    """Drop a judgment field the location's relation does not take.

    A ``convention`` belongs to a ``closeMatch`` (and to a part whose conjunct relation is
    one), a ``step`` to a ``hasPart`` or ``narrowMatch``, a ``conjunct_relation`` to a
    ``hasPart``, and ``tests`` to a location held to a strong relation — so a verdict that
    leaves a strong part keeps its tests and one that leaves it a ``narrowMatch`` drops
    them.
    """
    if loc.get("relation") != vocab.HAS_PART:
        loc.pop("conjunct_relation", None)
    if loc.get("relation") != vocab.CLOSE_MATCH and \
            loc.get("conjunct_relation") != vocab.CLOSE_MATCH:
        loc.pop("convention", None)
    if loc.get("relation") not in vocab.STEP_RELATIONS:
        loc.pop("step", None)
    if held_relation(loc) not in vocab.STRONG_RELATIONS:
        loc.pop("tests", None)
    if loc.get("relation") != vocab.HAS_PART:
        loc.pop("pd6", None)
    if loc.get("relation") != vocab.RELATED_MATCH:
        for key in vocab.BRIDGE_FIELDS:
            loc.pop(key, None)


def _apply_coverage(row: dict, planned: dict, verdicts: dict, problems: list[str], *,
                    status_rule: int = vocab.STATUS_RULE_D1) -> list[dict]:
    """A coverage claim's challenge, and the parts it confirms.

    Every claim is refuted by default: ``confirm`` makes it ``survived``, and its parts
    are returned for :func:`confirm_parts`, which the caller runs once the passage-level
    records are applied — after a prominence overturn's :func:`_reopen`, so a part the
    reopen made pending stays pending. ``refute`` removes it (kept under
    ``coverage_refuted``) and leaves its parts plain ``hasPart`` locations. A verdict
    applies only while the claim names the parts the plan saw. A claim whose part a
    verdict refuted or downgraded out of ``hasPart`` no longer holds: it upgrades nothing,
    confirms no part, and the passage carries a problem until the claim is challenged
    again.

    Under status rule 2 and 3 the claim is a bundle: a confirm's ``part_relations`` are
    kept beside it, a refute's ``unstated`` and ``part_reasons`` on ``coverage_refuted``,
    and a standing claim gets its ``relation``, its weakest part's.
    """
    sid, item, claim = row["source_id"], row["item"], row["coverage"]
    key = record_key(vocab.COVERAGE_TARGET, sid, item)
    plan = planned.get(key)
    verdict = verdicts.get(key)
    if plan is not None and sorted(plan.get("parts") or []) != sorted(claim["parts"]):
        claim["challenge"] = None
        _problem(row, problems, f"{sid} {item}: the coverage claim was planned with other "
                                "parts; it is owed again")
    elif verdict is not None and verdict.get("action") not in vocab.COVERAGE_ACTIONS:
        claim["challenge"] = None
        _problem(row, problems, f"{sid} {item}: a coverage verdict is confirm or refute, not "
                                f"{verdict.get('action')!r}; the claim is owed")
    else:
        claim["challenge"] = _challenge_value(key, planned, verdicts)
        if verdict is not None:
            row["runs"]["challenge"] = verdict["challenge_run"]
            if isinstance(verdict.get("claims"), list):
                row["challenge_claims"] = verdict["claims"]
            if verdict.get("action") == "confirm" and \
                    isinstance(verdict.get("part_clauses"), dict):
                # the challenger's clauses of each part, beside the claim: the claim itself
                # keeps exactly its record keys
                row["coverage_challenge_clauses"] = verdict["part_clauses"]
            if vocab.reads_bundles(status_rule) and verdict.get("action") == "confirm" \
                    and isinstance(verdict.get("part_relations"), dict):
                # the challenger's relation of each part to its conjuncts, which counts in
                # the bundle's relation where it is weaker than the part's own
                row["coverage_part_relations"] = verdict["part_relations"]
            for extra in ("part_conventions", "part_limits"):
                # the readings a confirm credits the parts with, kept for the audit
                if vocab.reads_bundles(status_rule) and verdict.get("action") == "confirm" \
                        and isinstance(verdict.get(extra), dict):
                    row[f"coverage_{extra}"] = verdict[extra]
            if verdict.get("action") == "refute":
                row["coverage_refuted"] = {**claim, "challenge": vocab.CHALLENGE_DOWNGRADED,
                                           "witness": verdict.get("witness", "")}
                for extra in ("unstated", "part_reasons"):
                    if vocab.reads_bundles(status_rule) and verdict.get(extra) is not None:
                        row["coverage_refuted"][extra] = verdict[extra]
                row["coverage"] = None
                row["passage_challenge_override"] = vocab.CHALLENGE_DOWNGRADED
                return []
    standing = {loc["location_key"]: loc for loc in row["locations"]
                if loc["relation"] == vocab.HAS_PART}
    gone = [key for key in claim["parts"] if key not in standing]
    if gone:
        named = {loc["location_key"]: f"{loc['library']}:{loc['declaration']}"
                 for loc in [*row["locations"], *row["refuted"]]}
        row["coverage_lapsed"] = True
        _problem(row, problems, f"{sid} {item}: the coverage claim names "
                                f"{', '.join(named.get(k, k[:12]) for k in gone)}, which the "
                                "challenge refuted or downgraded; the claim no longer holds and "
                                "is to be challenged again")
        return []
    if vocab.reads_bundles(status_rule) and not _bundle_stands(row, claim, standing, problems):
        return []
    if claim["challenge"] == vocab.CHALLENGE_SURVIVED:
        return [standing[key] for key in claim["parts"]]
    return []


def bundle_relation(parts: Iterable[dict], part_relations: dict | None = None) -> str | None:
    """A bundle's relation: its weakest part's.

    For each part, its ``conjunct_relation`` as the locate document wrote it (or as a
    verdict weakened it), and, once a challenger confirmed the claim, that confirm's
    ``part_relations`` entry for it — the weaker of the two kept, since a challenge never
    raises a relation; then the weakest over the parts. None when a part has no conjunct
    relation at all. A bundle of an ``exactMatch`` and a ``broadMatch`` part is a
    ``broadMatch`` bundle.
    """
    weakest: str | None = None
    for loc in parts:
        own = loc.get("conjunct_relation")
        if own not in vocab.CONJUNCT_RELATIONS_PART:
            return None
        given = (part_relations or {}).get(loc.get("location_key"))
        relation = own
        if given in vocab.CONJUNCT_RELATIONS_PART and \
                RELATION_STRENGTH[given] > RELATION_STRENGTH[own]:
            relation = given
        if weakest is None or RELATION_STRENGTH[relation] > RELATION_STRENGTH[weakest]:
            weakest = relation
    return weakest


def _bundle_stands(row: dict, claim: dict, standing: dict, problems: list[str]) -> bool:
    """Put the bundle's relation on a standing claim, or lapse the claim."""
    sid, item = row["source_id"], row["item"]
    why = ""
    if not row.get("conjuncts"):
        why = ("a bundle stands only on a compound statement, and the inventory gives this "
               "passage no conjuncts")
    else:
        relation = bundle_relation(
            [standing[key] for key in claim["parts"]],
            row.get("coverage_part_relations")
            if claim.get("challenge") == vocab.CHALLENGE_SURVIVED else None)
        if relation is None:
            why = "a part of the bundle names no conjunct_relation"
        elif relation == vocab.HAS_PART:
            why = ("its weakest part states only part of its conjuncts (a hasPart of them), "
                   "which leaves them unstated")
        elif relation not in vocab.BUNDLE_RELATIONS:
            why = (f"its weakest part is a {relation} of its conjuncts, a special case that "
                   "leaves them unstated")
        else:
            claim["relation"] = relation
            return True
    row["coverage_lapsed"] = True
    _problem(row, problems, f"{sid} {item}: the bundle no longer holds: {why}; it upgrades "
                            "nothing and is to be challenged again")
    return False


def confirm_parts(parts: Iterable[dict]) -> None:
    """A confirmed coverage claim confirms its parts.

    The challenger matched every clause of the passage to a part, so each part the plan
    never required was judged with the claim: one whose own record is ``not_required`` or
    ``sampled_out`` is ``survived``. A part a verdict judged keeps that verdict, an owed
    one stays owed, and so does a ``challenge_pending`` part — a prominence overturn in
    the same run put it under a rule that requires its own verdict, and the claim stands
    in only for parts no rule required.
    """
    for loc in parts:
        if loc.get("challenge") in vocab.CONFIRMED_WITH_CLAIM:
            loc["challenge"] = vocab.CHALLENGE_SURVIVED


def _reopen(row: dict) -> None:
    """A prominence overturned to ``subject``/``central`` reopens the passage's records.

    The plan judged the passage's records under ``supporting``: its locations were sampled
    or not required, its absence was not hunted. Under the new prominence every location
    is refuted by default and the absence is hunted, so each record no verdict judged
    becomes ``challenge_pending`` — never left ``not_required``, which a consumer would
    read as "no rule applied". A record a verdict judged keeps its value. It runs before a
    confirmed claim confirms its parts, so a part it makes pending is not confirmed.
    """
    for loc in row["locations"]:
        if loc.get("challenge") in vocab.CHALLENGE_UNJUDGED:
            loc["challenge"] = vocab.CHALLENGE_PENDING
    if row.get("record_challenge", "unset") in vocab.CHALLENGE_UNJUDGED:
        row["record_challenge"] = vocab.CHALLENGE_PENDING
    if row.get("absence") is not None and \
            row["absence"].get("challenge") in vocab.CHALLENGE_UNJUDGED:
        row["absence"]["challenge"] = vocab.CHALLENGE_PENDING
    if row.get("coverage") is not None and \
            row["coverage"].get("challenge") in vocab.CHALLENGE_UNJUDGED:
        row["coverage"]["challenge"] = vocab.CHALLENGE_PENDING


def _unchallenged(row: dict) -> None:
    """An unchallenged run's records: each one a challenge would owe is ``not_required``.

    No challenge run planned or judged anything, so every record is left owed (``None``)
    by an empty plan; sealed unchallenged, it reads ``not_required`` instead, the value a
    plan gives a record no rule required and the pages show as unchallenged. A coverage
    claim stamped so upgrades nothing and confirms no part, as any claim no challenge
    confirmed.
    """
    for loc in row["locations"]:
        if loc.get("challenge") is None:
            loc["challenge"] = vocab.CHALLENGE_NOT_REQUIRED
    if row.get("record_challenge", "unset") is None:
        row["record_challenge"] = vocab.CHALLENGE_NOT_REQUIRED
    if row.get("absence") is not None and row["absence"].get("challenge") is None:
        row["absence"]["challenge"] = vocab.CHALLENGE_NOT_REQUIRED
    if row.get("coverage") is not None and row["coverage"].get("challenge") is None:
        row["coverage"]["challenge"] = vocab.CHALLENGE_NOT_REQUIRED


def _renumbered(locations: list[dict], *,
                status_rule: int = vocab.STATUS_RULE_D1) -> list[dict]:
    """The surviving locations, ranked 1..n again.

    A refuted location leaves the passage and the ones after it move up: the export's
    ranks are the order a reader sees, never a gap where a refuted declaration stood.
    That half binds every run.

    Under status rule 3 they are ordered by relation strength first, so a downgrade moves
    its card below every card it is now weaker than: rank 1 after a challenge is the
    passage's strongest surviving card. **Among cards of equal strength the worker's order
    stands** — which declaration is stated for the passage's own objects is nowhere in a
    record, so nothing here can re-apply the specificity half of the rule.
    """
    strength = ((lambda loc: RELATION_STRENGTH.get(loc.get("relation"), 9))
                if vocab.ranks_by_strength(status_rule) else (lambda loc: 0))
    ordered = sorted(locations, key=lambda loc: (strength(loc), loc.get("rank") or 99,
                                                 loc["library"], loc["declaration"]))
    for rank, loc in enumerate(ordered, 1):
        loc["rank"] = rank
    return ordered


def _overturned(new: list[dict], old: list[dict]) -> list[dict]:
    """An overturn's locations: the challenger's first, then the kept ``relatedMatch`` ones.

    Renumbered 1..n and capped, so rank 1 is the challenger's best and never a related
    location the absence sat beside.
    """
    ordered = sorted(new, key=lambda loc: (loc.get("rank") or 99, loc["library"],
                                           loc["declaration"]))
    seen = {loc["location_key"] for loc in ordered}
    ordered += [loc for loc in sorted(old, key=lambda loc: (loc.get("rank") or 99, loc["library"],
                                                            loc["declaration"]))
                if loc["relation"] == vocab.RELATED_MATCH and loc["location_key"] not in seen]
    ordered = ordered[:vocab.MAX_LOCATIONS]
    for rank, loc in enumerate(ordered, 1):
        loc["rank"] = rank
    return ordered


# ---- the status --------------------------------------------------------------------------


def coverage_projection(claim: dict, *, status_rule: int = vocab.STATUS_RULE_D1) -> dict:
    """A coverage claim as the status rules read it: ``{complete, parts, challenge}``.

    Exactly what a record will carry of the claim, with the parts in the claim's order
    and the ``note`` left out — the status a record carries must be the one its own claim
    gives, so the derivation reads this projection and not the tally's working claim.
    Under status rules 2 and 3 it also carries the bundle's ``relation``, its weakest
    part's, which the derivation reads. The exported bundle, which adds the conjuncts and
    the ``bundle_key``, is :func:`records.coverage_ext`; that one is the export's and
    recomputes its key.
    """
    out = {"complete": True, "parts": [str(key) for key in claim["parts"]],
           "challenge": str(claim.get("challenge"))}
    if vocab.reads_bundles(status_rule):
        out["relation"] = claim.get("relation")
    return out


def _derive(row: dict, problems: list[str], *,
            status_rule: int = vocab.STATUS_RULE_D1) -> None:
    row["challenge"] = None if row["missing"] else passage_challenge(row)
    if row["outcome"] in (None, vocab.OUTCOME_UNRESOLVED) or row["missing"]:
        row["status"] = None
        return
    joined = [loc for loc in row["locations"] if loc.get("proof_status")]
    # the claim as its records will carry it, so the status is the one the exported claim
    # gives; an owed claim is no record's claim and upgrades nothing, and a lapsed one is
    # named as a problem already
    claim = row.get("coverage")
    coverage = None
    if claim is not None and claim.get("challenge") is not None \
            and not row.get("coverage_lapsed") and len(joined) == len(row["locations"]):
        coverage = coverage_projection(claim, status_rule=status_rule)
    try:
        row["status"] = status_mod.derive(row["passage_kind"] or "other", joined,
                                          row["absence"] is not None, coverage,
                                          status_rule=status_rule)
    except status_mod.StatusProblem as exc:
        _problem(row, problems, f"{row['source_id']} {row['item']}: {exc}")
        row["status"] = None
    if vocab.reads_bundles(status_rule):
        _owed_hunt(row, problems)


def _owed_hunt(row: dict, problems: list[str]) -> None:
    """A located passage the challenge left with nothing that counts is owed a hunt.

    Under status rule 2 and 3 only: a ``located`` passage that counted before the
    challenge and is left with no counting location and no absence has no status, and
    nobody checked that it is absent. Its challenge is ``challenge_pending`` unless a
    record is owed outright, it carries ``owed: "hunt"``, and a problem names it, so the
    export refuses it by name and the tally counts it. Under status rule 1 the same
    passage stays what it always was: no status, exported. A challenge document's
    ``fills`` make the case unreachable; this is the net under them.
    """
    if row["outcome"] != vocab.OUTCOME_LOCATED or row.get("absence") is not None \
            or not row.get("counted_before") \
            or any(_counts_toward_status(loc) for loc in row["locations"]):
        return
    if row["challenge"] is not None:
        row["challenge"] = vocab.CHALLENGE_PENDING
    row["owed"] = vocab.CHALLENGE_RULE_HUNT
    _problem(row, problems, f"{row['source_id']} {row['item']}: the challenge left this located "
                            "passage with no location that counts and no absence: it is owed a "
                            "hunt, and is not exported until one is recorded")


# ---- the whole run -------------------------------------------------------------------------


def build(workspace: Workspace, run_id: str, *, challenge_runs: Iterable[str] = (),
          universe: Universe | None = None, declarations=None,
          manifest: dict[str, tuple] | None = None, require_gate: bool = False,
          unchallenged: bool = False) -> dict:
    """Everything a run's harvest yields: rows, passage outcomes, counts, problems.

    ``{"run", "mode", "rows", "outcomes", "counts", "problems", "gate", "gates",
    "challenge_runs", "unchallenged", "sourceset", "universe", "status_rule"}``;
    ``outcomes`` is empty for an inventory run, and ``gates`` is the receipt summary of
    every run read. ``unchallenged`` seals a locate run with no challenge
    (:func:`_unchallenged`), and is refused beside a challenge run.
    """
    challenge_runs = list(challenge_runs)
    if unchallenged and challenge_runs:
        raise TallyProblem(f"run {run_id} is tallied under its challenge runs "
                           f"({', '.join(challenge_runs)}) or unchallenged, not both")
    run = workspace.run_config(run_id)
    loc_block = run["locate"]
    mode = str(loc_block.get("mode") or run.get("mode"))
    status_rule = status_rule_of(run)
    sourceset_id = str((loc_block.get("sourceset") or {}).get("id") or "")
    uni_pin = loc_block.get("universe") or {}
    if universe is not None and uni_pin.get("sha256") and universe.sha256 != uni_pin["sha256"]:
        raise TallyProblem(f"universe {universe.id} changed since run {run_id} was planned "
                           f"({str(uni_pin['sha256'])[:12]} -> {universe.sha256[:12]}): a run is "
                           "judged against the universe frozen into its plan")
    docs, gate = _documents(workspace, run_id, require_gate=require_gate)
    gates = {run_id: {"role": mode, **gate}}
    problems: list[str] = []
    result: dict[str, Any] = {
        "run_id": run_id, "mode": mode, "rows": [], "outcomes": [], "problems": problems,
        "gate": gate, "gates": gates, "challenge_runs": challenge_runs,
        "unchallenged": unchallenged, "sourceset": loc_block.get("sourceset"),
        "universe": uni_pin, "status_rule": status_rule}
    if mode == vocab.MODE_INVENTORY:
        result["rows"] = finding_rows(run_id, docs, sourceset_id=sourceset_id, universe=universe)
        result["counts"] = _inventory_counts(docs, result["rows"])
        return result
    if mode != vocab.MODE_LOCATE:
        raise TallyProblem(f"run {run_id} is a {mode} run; outcomes are built from a locate run, "
                           "with its challenge runs named")
    if universe is None:
        raise TallyProblem(f"run {run_id} needs its universe: pass the universe.json its plan "
                           "pinned")
    pins = manifest or {}
    inv_run = str(loc_block.get("inventory_run") or "")
    frozen: dict = {}
    if inv_run:
        inv = inventory(workspace, inv_run, require_gate=require_gate)
        frozen = inv["passages"]
        gates[inv_run] = {"role": vocab.MODE_INVENTORY, **inv["gate"]}
    planned = _planned(workspace, run_id, loc_block)
    c_conditional: dict = {}
    c_fills: dict = {}
    c_planned, c_verdicts, c_problems, c_gates = challenge_values(
        workspace, run_id, challenge_runs, require_gate=require_gate,
        conditional=c_conditional, fills=c_fills)
    problems.extend(c_problems)
    gates.update({rid: {"role": vocab.MODE_CHALLENGE, **g} for rid, g in c_gates.items()})

    accepted: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    outcomes: list[dict] = []
    for rel, doc in docs:
        if doc.get("mode") != vocab.MODE_LOCATE:
            problems.append(f"{rel} in locate run {run_id} is a {doc.get('mode')!r} document")
            continue
        sid = corpus_mod.source_of(rel, doc)
        doc_problems = []
        if doc.get("universe") != universe.id or doc.get("universe_sha256") != universe.sha256:
            doc_problems.append(f"{rel}: names universe {doc.get('universe')!r}, not "
                                f"{universe.id} at {universe.sha256[:12]}")
        pin = pins.get(sid)
        if pin is not None and (doc.get("source_sha"), doc.get("layer_sha256")) != pin:
            doc_problems.append(f"{rel}: source_sha/layer_sha256 are not the rendering the run "
                                f"pinned for {sid}")
        for passage in doc.get("passages") or []:
            item = str(passage.get("item") or "")
            key = (sid, items.join_key(item))
            want = (planned.get(key) or {}).get("task_id")
            if planned and want and want != corpus_mod.task_of(rel):
                problems.append(f"{rel}: passage {item} belongs to task {want}, not "
                                f"{corpus_mod.task_of(rel)}; ignored")
                continue
            if key in seen:
                problems.append(f"{rel}: passage {item} is located twice in the run")
                continue
            seen.add(key)
            accepted.add((rel, item))
            if planned and key not in planned:
                problems.append(f"{rel}: passage {item} is not one the run planned")
            base = frozen.get(key) or planned.get(key) or {}
            row = _outcome(run_id, sid, item, passage, doc, base, universe, declarations,
                           problems, task_id=corpus_mod.task_of(rel), inv_run=inv_run,
                           status_rule=status_rule)
            for text in doc_problems:
                _problem(row, problems, text)
            outcomes.append(row)
    for key, plan in sorted(planned.items()):
        if key in seen:
            continue
        base = frozen.get(key) or plan
        outcomes.append(_missing(run_id, key[0], plan["item"], base, plan,
                                 status_rule=status_rule))
    run_sources = set(loc_block.get("source_ids") or []) or {key[0] for key in planned}
    for key, base in sorted(frozen.items()):
        if key in seen or key in planned or base.get("scope") in vocab.LOCATED_SCOPES:
            continue
        if key[0] not in run_sources:
            continue
        outcomes.append(_scoped_out(run_id, key[0], base["item"], base, inv_run,
                                    status_rule=status_rule))

    conditional_of: dict[tuple[str, str], dict] = {}
    for key, verdict in c_conditional.items():
        conditional_of.setdefault(
            (verdict["source_id"], items.join_key(str(verdict.get("item") or ""))),
            {})[key] = verdict
    rows_of = {(row["source_id"], items.join_key(row["item"])) for row in outcomes}
    for pkey, fill in sorted(c_fills.items()):
        if pkey not in rows_of:
            problems.append(f"{fill['document']}: a fill for {fill['source_id']} {fill['item']}, "
                            f"which is no passage of run {run_id}; ignored")
    for row in outcomes:
        pkey = (row["source_id"], items.join_key(row["item"]))
        _apply_challenge(row, c_planned, c_verdicts, universe, declarations, problems,
                         workspace.layer, status_rule=status_rule,
                         conditional=conditional_of.get(pkey), fill=c_fills.get(pkey))
        if unchallenged:
            _unchallenged(row)
        _derive(row, problems, status_rule=status_rule)
        for scratch in ("record_challenge", "prominence_challenge",
                        "passage_challenge_override", "coverage_lapsed", "counted_before"):
            row.pop(scratch, None)
    outcomes.sort(key=lambda row: (row["source_id"], row["span"][0] if row.get("span") else -1,
                                   row["item"]))

    result["rows"] = finding_rows(run_id, docs, sourceset_id=sourceset_id, universe=universe,
                                  accepted=accepted)
    result["outcomes"] = outcomes
    result["counts"] = _locate_counts(outcomes, result["rows"], status_rule=status_rule)
    return result


# ---- the counts ----------------------------------------------------------------------------


def _inventory_counts(docs, rows) -> dict:
    kinds = Counter(row["kind"] for row in rows)
    return {"documents": len(docs), "passages": kinds.get(vocab.ROW_PASSAGE, 0),
            "unresolved_documents": kinds.get(vocab.ROW_UNRESOLVED, 0),
            "attempt_capped": sum(1 for row in rows
                                  if row.get("by") == vocab.UNRESOLVED_ATTEMPT_CAP),
            "by_scope": dict(sorted(Counter(row.get("scope") for row in rows
                                            if row["kind"] == vocab.ROW_PASSAGE).items())),
            "by_passage_kind": dict(sorted(Counter(row.get("passage_kind") for row in rows
                                                   if row["kind"] == vocab.ROW_PASSAGE).items()))}


def _locate_counts(outcomes: list[dict], rows: list[dict], *,
                   status_rule: int = vocab.STATUS_RULE_D1) -> dict:
    answered = [row for row in outcomes if row["outcome"] is not None]
    by_library = Counter(loc["library"] for row in outcomes for loc in row["locations"])
    counts = {
        "passages": len(outcomes),
        "by_outcome": dict(sorted(Counter(row["outcome"] or "none"
                                          for row in outcomes).items())),
        "by_status": dict(sorted(Counter(row["status"] or "none" for row in answered).items())),
        "by_library": dict(sorted(by_library.items())),
        "by_challenge": dict(sorted(Counter(row["challenge"] or "owed"
                                            for row in answered).items())),
        # an answered passage with no status counts as unresolved: the worker's, and one a
        # challenge left without a location that counts
        "unresolved": sum(1 for row in answered if row["status"] is None),
        "attempt_capped": sum(1 for row in outcomes
                              if row.get("unresolved_by") == vocab.UNRESOLVED_ATTEMPT_CAP),
        "missing": sum(1 for row in outcomes if row["missing"]),
        "scoped_out": sum(1 for row in outcomes
                          if row["outcome"] is None and not row["missing"]),
        "problems": sum(1 for row in outcomes if row.get("problem")),
        "rows": dict(sorted(Counter(row["kind"] for row in rows).items())),
    }
    if vocab.reads_bundles(status_rule):
        counts.update(_bundle_counts(outcomes))
    return counts


def _bundle_counts(outcomes: list[dict]) -> dict:
    """The counts status rules 2 and 3 add; every other count is status rule 1's."""
    written = [row for row in outcomes if row.get("coverage") or row.get("coverage_refuted")]
    # a claim gets its relation only while it holds: a lapsed one has none
    survived = [row for row in outcomes
                if (row.get("coverage") or {}).get("challenge") == vocab.CHALLENGE_SURVIVED
                and (row.get("coverage") or {}).get("relation")]
    return {
        "owed_hunt": sum(1 for row in outcomes if row.get("owed") == vocab.CHALLENGE_RULE_HUNT),
        "by_cut_flag": dict(sorted(Counter(row["cut_flag"] for row in outcomes
                                           if row.get("cut_flag")).items())),
        "bundles": {"written": len(written), "survived": len(survived),
                    "by_relation": dict(sorted(Counter(row["coverage"]["relation"]
                                                       for row in survived).items()))},
    }


# ---- what gets written ------------------------------------------------------------------------


#: The keys an outcome row carries in ``locate-outcomes.<run>.jsonl``, in this order.
OUTCOME_KEYS = ("source_id", "item", "passage_kind", "prominence", "scope", "outcome",
                "locations", "coverage", "absence", "challenge", "status", "runs", "span",
                "head_words", "section", "source_sha", "layer_sha256", "reason",
                "unresolved_by", "missing", "refuted", "coverage_refuted", "task_id")
#: Keys written only where a document wrote one, so a run planned before the judgment
#: rules writes exactly the rows it always did.
OUTCOME_OPTIONAL = ("claims", "challenge_claims", "challenge_receipt",
                    "coverage_challenge_clauses", "coverage_part_relations",
                    "coverage_part_conventions", "coverage_part_limits", *STATEMENT_FIELDS,
                    "cut_flag", "owed", "fill")


def outcome_row(row: dict) -> dict:
    """The row as written to ``reports/locate-outcomes.<run>.jsonl``."""
    out = {key: row.get(key) for key in OUTCOME_KEYS}
    for key in OUTCOME_OPTIONAL:
        if row.get(key) is not None:
            out[key] = row[key]
    if row.get("problem"):
        out["problem"] = row["problem"]
    return out


def bundle_conjuncts(row: dict, claim: dict) -> list[dict]:
    """``[{id, text, part}]``: each conjunct of the passage beside the part that states it.

    The passage's conjuncts come from the frozen inventory, in id order; the part is the
    one location of the claim whose ``clauses`` name that conjunct. A bundle stands only
    on a compound statement whose parts partition its conjuncts, so a passage with no
    conjuncts, a conjunct no part names or one two parts name is refused — the export
    would have no part to show the conjunct beside.

    It is computed here and not in the exporter because both halves are the tally's: the
    conjuncts are the inventory's and the clauses are the worker's or the challenger's.
    """
    conjuncts = row.get("conjuncts")
    item = row.get("item")
    if not isinstance(conjuncts, (list, tuple)) or not conjuncts:
        raise TallyProblem(f"{item}: a bundle on a passage with no conjuncts (a coverage claim "
                           "stands only on a compound statement)")
    by_key = {str(loc.get("location_key")): loc for loc in row.get("locations") or []}
    out = []
    for conjunct in conjuncts:
        cid = conjunct.get("id") if isinstance(conjunct, dict) else None
        stating = [str(key) for key in claim["parts"]
                   if cid in ((by_key.get(str(key)) or {}).get("clauses") or [])]
        if len(stating) != 1:
            raise TallyProblem(f"{item}: conjunct {cid!r} is stated by {len(stating)} part(s) "
                               "of the bundle, not exactly one")
        out.append({"id": str(cid), "text": str(conjunct.get("text") or ""),
                    "part": stating[0]})
    named = {entry["part"] for entry in out}
    idle = [str(key) for key in claim["parts"] if str(key) not in named]
    if idle:
        raise TallyProblem(f"{item}: bundle part {idle[0][:12]} states no conjunct (a bundle's "
                           "parts partition its conjuncts, each stating at least one)")
    return out


def export_rows(built: dict, workspace: Workspace) -> tuple[list[dict], list[str]]:
    """``(rows, refusals)``: the built outcomes in the shape stage 6's exporter takes.

    ``export_records.py`` reads the rows ``readback.py`` produces — the same passage row
    with its ``cut`` beside it — so this is where the tally's row becomes the export's.
    The cut is **derived**, not carried: :func:`records.cut_passage` cuts the passage's
    span out of the source's rendered text layer, which is the one value in a record only
    the layer can give. A passage this host holds no layer for, one with a problem on it,
    or one the cut refuses is left out and named: the export would refuse it anyway, and
    silently short rows is how a set comes out "cleaner" than the run.
    """
    out: list[dict] = []
    refused: list[str] = []
    for row in built.get("outcomes") or []:
        if row.get("outcome") is None or row.get("problem"):
            continue
        sid, item = row["source_id"], row["item"]
        if not row.get("span"):
            refused.append(f"{sid} {item}: no span, so the passage cannot be cut")
            continue
        layer = workspace.layer(sid)
        if layer is None:
            refused.append(f"{sid} {item}: no rendered layer for {sid}, so the passage cannot "
                           "be cut")
            continue
        try:
            cut = records_mod.cut_passage(layer, row["span"])
        except records_mod.RecordProblem as exc:
            refused.append(f"{sid} {item}: {exc}")
            continue
        made = {**{key: row[key] for key in OUTCOME_KEYS if key in row},
                **{key: row[key] for key in OUTCOME_OPTIONAL if row.get(key) is not None},
                "cut": {"quote": dict(cut.quote), "span_sha256": cut.span_sha256}}
        claim = made.get("coverage")
        if isinstance(claim, dict) and vocab.reads_bundles(built["status_rule"]):
            # the bundle its records carry: its conjuncts beside the parts that state
            # them, which the exporter writes and recomputes the bundle key over. The
            # outcome row keeps the claim the tally worked with; this is the record's.
            try:
                made["coverage"] = {**claim, "conjuncts": bundle_conjuncts(row, claim)}
            except TallyProblem as exc:
                refused.append(str(exc))
                continue
        out.append(made)
    return out, refused


def report(built: dict) -> dict:
    """The tally report: everything but the outcome rows, which go to their own file."""
    return {"run_id": built["run_id"], "mode": built["mode"], "sourceset": built["sourceset"],
            "universe": built["universe"], "challenge_runs": built["challenge_runs"],
            "unchallenged": built["unchallenged"],
            "gate": built["gate"], "gates": built["gates"], "counts": built["counts"],
            "problems": built["problems"], "rows": built["rows"]}


# ---- the command line --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="the locate or inventory run to tally")
    parser.add_argument("--corpus", required=True, type=Path,
                        help="the corpus root; the run's documents are <corpus>/<run-id>/")
    parser.add_argument("--runs", required=True, type=Path,
                        help="the runs root; the run's plan is <runs>/<run-id>/")
    parser.add_argument("--universe", type=Path,
                        help="universe.json, the universe the run's plan pinned")
    parser.add_argument("--index", type=Path,
                        help="the declaration index the proof statuses are joined from "
                             "(.jsonl snapshot or SQLite)")
    parser.add_argument("--challenge-run", action="append", default=[],
                        help="a challenge run whose verdicts apply (repeatable)")
    parser.add_argument("--unchallenged", action="store_true",
                        help="tally a locate run that no challenge ran over: every record "
                             "a challenge would judge is not_required, which the pages read "
                             "as unchallenged. Refused beside --challenge-run")
    parser.add_argument("--manifest", type=Path,
                        help="the sourceset manifest the run pinned, to check each document's "
                             "source_sha and layer_sha256")
    parser.add_argument("--sources", type=Path,
                        help="the sourceset's rendered sources, so a downgrade's note can be "
                             "held to the quote cap")
    parser.add_argument("--require-gate", action="store_true",
                        help="refuse a run whose harvest is not complete and gated")
    parser.add_argument("--outcomes", type=Path, help="write the outcome rows here (JSONL)")
    parser.add_argument("--report", type=Path, help="write the tally report here (JSON)")
    parser.add_argument("--export-rows", type=Path,
                        help="write the rows stage 6's exporter takes here (JSONL)")
    args = parser.parse_args(argv)

    workspace = Workspace(args.corpus, args.runs, args.sources)
    universe = Universe.load(args.universe) if args.universe else None
    declarations = index_mod.open_index(
        args.index, universe_sha256=universe.sha256 if universe else None)
    try:
        built = build(workspace, args.run_id, challenge_runs=args.challenge_run,
                      universe=universe, declarations=declarations,
                      manifest=read_manifest(args.manifest), require_gate=args.require_gate,
                      unchallenged=args.unchallenged)
    finally:
        declarations.close()

    if args.outcomes:
        args.outcomes.parent.mkdir(parents=True, exist_ok=True)
        args.outcomes.write_text(
            "".join(json.dumps(outcome_row(row), ensure_ascii=False) + "\n"
                    for row in built["outcomes"]), encoding="utf-8")
    if args.export_rows:
        rows, refused = export_rows(built, workspace)
        args.export_rows.parent.mkdir(parents=True, exist_ok=True)
        args.export_rows.write_text("".join(keys.canonical_line(row) for row in rows),
                                    encoding="utf-8")
        print(f"  export rows: {len(rows)} written, {len(refused)} not exportable")
        for text in refused[:10]:
            print(f"    {text}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report(built), indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")

    counts = built["counts"]
    print(f"{args.run_id} [{built['mode']}]: {len(built['rows'])} finding row(s)")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    for problem in built["problems"][:20]:
        print(f"  problem: {problem}")
    if len(built["problems"]) > 20:
        print(f"  ... and {len(built['problems']) - 20} more problem(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
