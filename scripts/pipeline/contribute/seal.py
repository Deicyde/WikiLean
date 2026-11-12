#!/usr/bin/env python3
"""The contributor's own seal: a harvested run becomes a sealed record set, here.

docs/PIPELINE.md section 10's last bullet — *"the contributor's own seal: stage 11
wires ``verify_submission.py`` in, stage 8 produces rows on the contributor's machine
and stage 9a now gives it the sourceset the export's source rows come from, so sealing
there is reachable; what is missing is the command that does it on the contributor's
side."* This is that command.

Three steps of WikiLean's own pipeline, in order, over the run the contributor just
finished:

1. **stage 8, the tally** (``scripts/pipeline/tally.py``): the harvested documents
   become outcome rows under the challenge's verdicts, or unchallenged when no challenge
   ran (every record ``not_required``), with the proof statuses joined from the
   declaration index, and then the rows the exporter takes — each passage's quote cut
   out of **this machine's** rendered layer, which is the one value only the layer can
   give;
2. **stage 6, the export** (``scripts/pipeline/export_records.py``): those rows become
   ``SEAL.json``, ``universe.json``, ``sources.jsonl``, ``records/*.jsonl`` and
   ``receipts/*.json`` — the same bytes the coordinator's own export would write from
   the same rows, which is what makes a re-export a check rather than a new claim;
3. **the intake contract** (``scripts/articles/verify_submission.py``): the set is run
   through the nine checks the submission endpoint will run, **on this machine, before
   anything is pushed**, so a refusal is seen here with the same code, the same HTTP
   status and the name of the rule it broke.

**Every pin comes from the package, not from this machine.** The universe, the
sourceset id and its manifest sha, the status rule and the campaign are the
coordinator's, carried in ``package.json`` and ``run.json``; the producer commit is the
WikiLean commit the contributor ran, which it is asked for rather than guessed at. A
seal is a statement about bytes and about who made them, and neither is a thing to
infer from a working directory.

**Nothing here is posted.** The set is written to a directory. ``run_package.py
--stage results --record-set <that directory>`` is what sends it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
ROOT = PIPELINE.parents[1]
for _path in (str(HERE), str(PIPELINE), str(PIPELINE / "run"),
              str(ROOT / "scripts" / "articles")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import corpus as corpus_mod  # noqa: E402
import export_records  # noqa: E402
import index as index_mod  # noqa: E402
import package as package_mod  # noqa: E402
import tally as tally_mod  # noqa: E402
import vocab  # noqa: E402
import work as work_mod  # noqa: E402

#: A 40-hex git commit, which is what a seal's ``skill_commit`` is.
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")


class SealProblem(ValueError):
    """A run this command will not seal, named rather than sealed anyway."""


#: Where a contributor's **own** runs live — the stage-9b cut it made and any challenge
#: run it made — beside the one package root the coordinator planned.
RUNS_DIR = "runs"


class ContributorWorkspace(tally_mod.Workspace):
    """The tally's workspace, over a contributor's layout rather than a run host's.

    A run host keeps ``runs/<run-id>/`` beside ``corpus/<run-id>/``. A contributor has
    **one** planned root, at ``<workdir>/root``, because it holds exactly one package —
    and its own runs, the cut it made and any challenge it ran, under
    ``<workdir>/runs/<run-id>/``. The only thing the tally asks of the runs root is
    where a run's plan is, so that is the one method overridden: the package's run id
    resolves to the root, everything else to the contributor's own runs directory.
    """

    def __init__(self, corpus_dir: Path, root: Path, run_id: str,
                 runs_dir: Path | None = None, sources_dir: Path | None = None):
        root = Path(root)
        super().__init__(corpus_dir, runs_dir or (root.parent / RUNS_DIR), sources_dir)
        self.root = root
        self.package_run = run_id

    def run_dir(self, run_id: str) -> Path:  # noqa: D102 - the one difference
        return self.root if run_id == self.package_run else self.runs_dir / run_id


def package_universe(package_dir: Path) -> Path:
    """``universe.json`` as the coordinator published it, which the seal pins.

    It is in the package and **not** in the materialised root: the root holds the plan,
    and the universe is part of the package's own description (``work.NOT_THE_PLAN``).
    """
    path = Path(package_dir) / package_mod.UNIVERSE_JSON
    if not path.is_file():
        raise SealProblem(f"the package carries no {package_mod.UNIVERSE_JSON} at {path}")
    return path


def package_sources(package_dir: Path) -> Path:
    """``sources.jsonl``, the sources' pins: one row per source, no bytes."""
    path = Path(package_dir) / package_mod.SOURCES_JSONL
    if not path.is_file():
        raise SealProblem(f"the package carries no {package_mod.SOURCES_JSONL} at {path}")
    return path


def pins(root: Path, manifest: dict) -> dict:
    """The seal's pins, read off the package and the plan. Nothing of this machine."""
    run = work_mod.run_config(root)
    locate = run.get("locate") or {}
    sourceset = locate.get("sourceset") or {}
    universe = locate.get("universe") or {}
    package_universe = manifest.get("universe") or {}
    out = {
        "run_id": str(manifest.get("run_id") or run.get("run_id") or ""),
        "status_rule": locate.get("status_rule"),
        "sourceset_id": sourceset.get("id"),
        "sourceset_manifest": sourceset.get("manifest_sha256"),
        "universe_id": universe.get("id") or package_universe.get("id"),
        "universe_sha256": (universe.get("sha256")
                            or package_universe.get("universe_sha256")),
    }
    if out["status_rule"] not in vocab.STATUS_RULES:
        raise SealProblem(
            f"the plan records status rule {out['status_rule']!r} and a set is exported "
            f"under one of {sorted(vocab.STATUS_RULES)}; this root is not a planned "
            "locate run")
    if not out["sourceset_manifest"]:
        raise SealProblem(
            "the plan records no sourceset manifest sha256, which a seal must carry; "
            "the coordinator's plan writes it into run.json's locate block")
    return out


def tally(*, root: Path, workdir: Path, run_id: str, index: Path, universe: Path,
          runs: Path | None = None, challenge_runs: tuple[str, ...] = (),
          unchallenged: bool = False) -> tuple[dict, list[dict], list[str]]:
    """Stage 8 over the contributor's own corpus: ``(built, export rows, refusals)``.

    ``require_gate`` is on and is not optional here: a contributor seals what the gate
    passed and the harvest reconciled, or it seals nothing. ``unchallenged`` tallies a
    run no challenge ran over (``tally.build``'s).
    """
    root, workdir = Path(root), Path(workdir)
    corpus = workdir / work_mod.CORPUS_DIR
    # A locate run is tallied against the frozen inventory it was cut from, and that
    # inventory is the contributor's own stage-9b run over the same pinned revisions —
    # not something the package can carry, because its passages are article text. Say
    # so here rather than letting the tally say it about a path nobody recognises.
    inventory_run = str(((work_mod.run_config(root).get("locate") or {})
                         .get("inventory_run")) or "")
    if inventory_run and not (corpus / inventory_run).is_dir():
        raise SealProblem(
            f"the plan was cut from inventory run {inventory_run!r} and this machine's "
            f"corpus has no {corpus / inventory_run}. A set is sealed against the "
            "inventory its passages were cut from: run the cut (stage 9b) over the "
            "same pinned revisions into this corpus, or ask the coordinator for the "
            "inventory run's harvested corpus.")
    workspace = ContributorWorkspace(corpus, root, run_id, runs_dir=runs,
                                     sources_dir=work_mod.sources_dir(root))
    frozen = tally_mod.Universe.load(Path(universe))
    declarations = index_mod.open_index(Path(index), universe_sha256=frozen.sha256)
    try:
        built = tally_mod.build(workspace, run_id, challenge_runs=list(challenge_runs),
                                universe=frozen, declarations=declarations,
                                require_gate=True, unchallenged=unchallenged)
    finally:
        declarations.close()
    rows, refused = tally_mod.export_rows(built, workspace)
    return built, rows, refused


def seal(*, root: Path, workdir: Path, package_dir: Path, manifest: dict, out: Path,
         set_id: str, campaign: str, producer_commit: str, created: str, index: Path,
         sourceset: str = "", challenge_runs: tuple[str, ...] = (),
         unchallenged: bool = False, runs: Path | None = None, byte_budget: int = 0,
         log=print) -> dict:
    """Tally and export. Returns what was sealed; the caller verifies it."""
    root, workdir, out = Path(root), Path(workdir), Path(out)
    if not COMMIT_RE.match(producer_commit or ""):
        raise SealProblem(
            f"--producer-commit is the 40-hex commit of the WikiLean checkout that "
            f"produced this set, and {producer_commit!r} is not one "
            "(`git -C <your clone> rev-parse HEAD`)")
    if unchallenged and challenge_runs:
        raise SealProblem("--unchallenged seals a run no challenge ran over, so it is "
                          "refused beside --challenge-run")
    if unchallenged and "+" in (campaign or ""):
        # the pages read a run token's second half as the challenge run, and this set
        # has none
        raise SealProblem(f"an unchallenged set's campaign names no challenge run: "
                          f"'run:<run>', not {campaign!r}")
    pinned = pins(root, manifest)
    run_id = pinned["run_id"]
    universe = package_universe(package_dir)
    built, rows, refused = tally(root=root, workdir=workdir, run_id=run_id,
                                 index=index, universe=universe, runs=runs,
                                 challenge_runs=challenge_runs, unchallenged=unchallenged)
    log(f"  tally    : {len(built['rows'])} finding row(s), {len(rows)} exportable")
    for text in refused[:10]:
        log(f"    not exportable: {text}")
    for problem in built["problems"][:10]:
        log(f"    problem: {problem}")
    if not rows:
        raise SealProblem(
            "the tally produced no exportable row, so there is nothing to seal. The "
            "usual cause is that this machine holds no rendered layer for a source "
            "(a passage's quote is cut from the layer), or that no document was gated.")

    report = export_records.build_set(
        rows=rows,
        sources=export_records._read_jsonl(package_sources(package_dir)),
        universe=json.loads(universe.read_text(encoding="utf-8")),
        out=out, set_id=set_id, campaign=campaign,
        # the plan records the sourceset's manifest sha and may record no id for it, so
        # the id is given or falls back to the run's: the sha is the pin that matters
        sourceset=sourceset or str(pinned["sourceset_id"] or run_id),
        sourceset_manifest=str(pinned["sourceset_manifest"]),
        producer_commit=producer_commit, created=created,
        status_rule=int(pinned["status_rule"]), byte_budget=byte_budget)
    exported = sum((report.get("exported") or {}).values())
    log(f"  sealed   : {out} — {exported} record(s) over "
        f"{len(report.get('exported') or {})} source(s), "
        f"{len(report.get('refused') or [])} row(s) refused, seal "
        f"{report['seal_sha256'][:12]}")
    for text in (report.get("refused") or [])[:10]:
        log(f"    refused: {text}")
    if not exported:
        # a set with no record is a set nobody should send, and the intake contract
        # would accept it: an empty set breaks none of its nine checks
        raise SealProblem(
            f"the export refused every row, so the set at {out} holds no record and "
            "is not worth sending. The usual cause is that no challenge run's verdicts "
            "were applied — nothing leaves this pipeline without a challenge value, so "
            "run stage 9d over this run and name it with --challenge-run, or seal it "
            "with --unchallenged, which the pages show as unchallenged. The export's "
            "own reasons are above.")
    return {"pins": pinned, "export": report, "rows": len(rows),
            "not_exportable": refused, "tally_problems": built["problems"],
            "set_dir": str(out)}
