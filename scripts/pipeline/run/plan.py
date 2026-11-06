#!/usr/bin/env python3
"""Stage 10: plan a batch, and write the root the agents run on.

docs/PIPELINE.md section 3 (stage 10). A locate batch is a deterministic function of its
inputs: the frozen inventory, the sourceset the run pins, the candidates the retrieval
handed each article, the universe and the per-task cap. Given the same inputs this writes
the same root, so a re-plan of an unchanged run is byte for byte the root it wrote before
and a contributor and a coordinator can check they planned the same thing.

The root:

    <root>/run.json                the run's own configuration (the tally reads it)
    <root>/planned.json            every task and the passages it was handed
    <root>/gate/                   the staged gate and its gate.json, out of tree
    <root>/tasks/<task-id>.json    one manifest per task
    <root>/prompts/*.md            the prompts, by sha
    <root>/work/<task-id>/         where a worker writes its document
    <root>/usage/                  the agents' envelopes, which the watchdog reads
    <root>/gate-log/               the gate's own log, which the watchdog reads

What is **not** here: the agent runner itself. tend, agentbox and the run host are a
separate tool a host has; WikiLean's pipeline drives whatever runs the calls rather than
vendoring it, exactly as the contribute transport does. :mod:`agent` is the one call,
and it goes through ``site/agent_sandbox.py``.

Ported from ``locate/plan.py`` and ``locate/tend_root.py``, narrowed to what a batch
needs: the task cut, the manifests, the gate's pins and the root's shape. The retrieval
that chooses candidates is stage 9c and does not move here.

    python3 scripts/pipeline/run/plan.py --run-id loc-1 --mode locate --out <root> \\
        --inventory <corpus dir> --universe <file> --manifest <file> \\
        --sources <dir> [--candidates <dir>] [--index <file>] [--per-task 8]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE, PIPELINE / "gate"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import corpus as corpus_mod  # noqa: E402
import items  # noqa: E402
import keys  # noqa: E402
import stage as stage_mod  # noqa: E402 — scripts/pipeline/gate/stage.py
import vocab  # noqa: E402

PLAN_SCHEMA = "wikilean.pipeline.plan/1"
RUN_SCHEMA = "wikilean.pipeline.run/1"
GATE_SCHEMA = "wikilean-gate-locate/1"
#: How many passages one task is handed by default.
PER_TASK = 8
#: The directories a root always has, so a runner never creates one by accident.
ROOT_DIRS = ("tasks", "work", "usage", "gate-log", "prompts")


class PlanProblem(ValueError):
    """An input a batch cannot be planned from, named rather than planned around."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> str:
    """Write canonical, sorted JSON and return its sha256: a re-plan writes these bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def frozen_inventory(corpus_dir: Path) -> dict[str, list[dict]]:
    """``{source_id: [passage]}`` of a harvested inventory run, in document order."""
    out: dict[str, list[dict]] = {}
    for rel, doc in corpus_mod.documents(Path(corpus_dir)):
        if doc.get("mode") != vocab.MODE_INVENTORY:
            raise PlanProblem(f"{rel} is a {doc.get('mode')!r} document; a batch is planned "
                              "from a locate-inventory run")
        sid = corpus_mod.source_of(rel, doc)
        if sid in out:
            raise PlanProblem(f"the inventory holds two documents for {sid}")
        out[sid] = [dict(passage) for passage in doc.get("passages") or []
                    if isinstance(passage, dict)]
    return out


def read_manifest(path: Path) -> dict[str, dict]:
    """``{source_id: entry}`` of a sourceset manifest, the last entry of each id winning."""
    out: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict) and entry.get("source_id"):
            out[str(entry["source_id"])] = entry
    return out


def candidates_of(directory: Path | None, source_id: str) -> list[dict]:
    """The curated hits the retrieval left for a source, or ``[]``."""
    if directory is None:
        return []
    path = Path(directory) / f"{source_id}.json"
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = value.get("curated") if isinstance(value, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def cut_tasks(source_id: str, passages: Iterable[dict], *, per_task: int,
              mode: str) -> list[list[dict]]:
    """A source's located passages, cut into tasks of at most ``per_task``.

    Only a ``formal_math`` passage is located; the rest are scoped out by the inventory
    and the tally keeps them without asking anyone. The order is the layer's, so a
    re-plan of an unchanged inventory cuts the same tasks.
    """
    if per_task < 1:
        raise PlanProblem(f"a task holds at least one passage, not {per_task}")
    wanted = [passage for passage in passages
              if passage.get("scope") in vocab.LOCATED_SCOPES] \
        if mode == vocab.MODE_LOCATE else list(passages)
    wanted.sort(key=lambda passage: ((passage.get("span") or [0])[0],
                                     str(passage.get("item"))))
    return [wanted[at:at + per_task] for at in range(0, len(wanted), per_task)]


def task_id(run_id: str, source_id: str, number: int) -> str:
    """``<run>-<source>-<n>``: one task, named so a document's path says which it is."""
    return f"{run_id}-{source_id}-{number}"


def plan(*, run_id: str, mode: str, out: Path, inventory: Path, universe: Path,
         manifest: Path, sources: Path, candidates: Path | None = None,
         index: Path | None = None, per_task: int = PER_TASK,
         inventory_run: str = "", status_rule: int = vocab.STATUS_RULE_BROAD,
         span_end_rule: int = 3, statement_cut_rule: int = 3, judgment_rules: int = 4,
         model: str = "", max_cost: float = 0.0, source_ids: Iterable[str] = (),
         prompts: Path | None = None, sources_as: str | None = None,
         candidates_as: str | None = None, index_as: str | None = None) -> dict:
    """Write the root and return its ``planned.json``.

    ``sources_as``, ``candidates_as`` and ``index_as`` are what ``gate.json`` **names**,
    where that is not where this machine holds them: a package carries the plan and no
    article text, so a coordinator plans against its own rendered sources and the plan
    names ``../sources``, which the contributor fills by refetching the pinned
    revisions. The view shas are still read from the real directory, so the plan pins
    the bytes the coordinator saw and the contributor's gate refuses anything else.
    """
    if mode not in (vocab.MODE_LOCATE, vocab.MODE_CHALLENGE):
        raise PlanProblem(f"a batch is planned in {vocab.MODE_LOCATE} or "
                          f"{vocab.MODE_CHALLENGE} mode, not {mode!r}")
    out = Path(out)
    if out.exists() and not out.is_dir():
        raise PlanProblem(f"{out} is not a directory")
    universe_doc = json.loads(Path(universe).read_text(encoding="utf-8"))
    entries = read_manifest(manifest)
    frozen = frozen_inventory(inventory)
    wanted = sorted(set(source_ids) or set(frozen))
    missing = [sid for sid in wanted if sid not in frozen]
    if missing:
        raise PlanProblem(f"the inventory holds no document for "
                          f"{', '.join(missing[:5])}")
    unpinned = [sid for sid in wanted if sid not in entries]
    if unpinned:
        raise PlanProblem(f"the sourceset manifest holds no entry for "
                          f"{', '.join(unpinned[:5])}")

    for name in ROOT_DIRS:
        (out / name).mkdir(parents=True, exist_ok=True)
    tasks: list[dict] = []
    planned_passages = 0
    for sid in wanted:
        for number, group in enumerate(cut_tasks(sid, frozen[sid], per_task=per_task,
                                                 mode=mode), 1):
            if not group:
                continue
            tid = task_id(run_id, sid, number)
            entry = entries[sid]
            manifest_doc = {
                "task_id": tid, "run_id": run_id, "mode": mode,
                "documents": [{"source_id": sid,
                               "source_sha": entry.get("html_sha256"),
                               "layer_sha256": entry.get("layer_sha256"),
                               "renderer": entry.get("renderer"),
                               "curated": candidates_of(candidates, sid),
                               "passages": group}],
            }
            tasks.append(manifest_doc)
            planned_passages += len(group)
            _write_json(out / "tasks" / f"{tid}.json", manifest_doc)
            (out / "work" / tid).mkdir(parents=True, exist_ok=True)

    def where(path: Path | None) -> str | None:
        """A path as ``gate.json`` names it: relative to the gate directory when it is
        inside the root, so the whole root moves to another machine unchanged, and
        absolute when it is not."""
        if path is None:
            return None
        resolved = Path(path).resolve()
        gate_dir = (out / "gate").resolve()
        root = out.resolve()
        if resolved == root or root in resolved.parents:
            return str(Path(os.path.relpath(resolved, gate_dir)).as_posix())
        return str(resolved)

    gate_sources = {}
    for sid in wanted:
        entry = entries[sid]
        views = {}
        for name in ("formulas.json", "paragraphs.json"):
            path = Path(sources) / sid / name
            if path.is_file():
                views[name] = sha256_file(path)
        gate_sources[sid] = {"html_sha256": entry.get("html_sha256"),
                             "layer_sha256": entry.get("layer_sha256"),
                             "renderer": entry.get("renderer"), "views": views}
    gate_config = {
        "schema": GATE_SCHEMA, "run_id": run_id, "mode": mode,
        "universe": {"id": universe_doc.get("id"),
                     "universe_sha256": universe_doc.get("universe_sha256"),
                     "libraries": {str(lib.get("id")): {"repo": lib.get("repo"),
                                                        "commit": lib.get("commit")}
                                   for lib in universe_doc.get("libraries") or []}},
        "sources": gate_sources,
        "sources_dir": sources_as or where(Path(sources)),
        "tasks_dir": "tasks",
        "candidates_dir": (candidates_as
                           or (where(Path(candidates)) if candidates else None)),
        "candidates_sha256": {},
        "findings_dir": "findings",
        "log_dir": where(out / "gate-log"),
        "blind": False,
        "max_locations": vocab.MAX_LOCATIONS,
        "min_absence_queries": vocab.MIN_ABSENCE_QUERIES,
        "span_end_rule": span_end_rule,
        "statement_cut_rule": statement_cut_rule,
        "judgment_rules": judgment_rules,
        "inventory": {"run_id": inventory_run} if inventory_run else {},
        "challenged_runs": [],
    }
    if index is not None:
        gate_config["index"] = {"path": index_as or where(Path(index)),
                                "sha256": sha256_file(index)}
    # gate.json lives in the gate directory and nowhere else: one pinned configuration,
    # in the one directory a worker cannot edit
    scratch = out / "gate.json"
    gate_config_sha = _write_json(scratch, gate_config)
    staged = stage_mod.stage(out / "gate", scratch, sorted((out / "tasks").glob("*.json")))
    scratch.unlink()

    prompt_shas: dict[str, str] = {}
    if prompts is not None:
        for path in sorted(Path(prompts).glob("*.md")):
            target = out / "prompts" / path.name
            target.write_bytes(path.read_bytes())
            prompt_shas[path.name] = sha256_file(target)

    planned = {"schema": PLAN_SCHEMA, "run_id": run_id, "mode": mode, "tasks": tasks}
    _write_json(out / "planned.json", planned)
    run_config = {
        "schema": RUN_SCHEMA, "run_id": run_id, "mode": mode, "model": model,
        "tasks": {"count": len(tasks), "documents": len(tasks),
                  "passages": planned_passages, "max_per_task": per_task},
        "budget": {"max_cost_usd": max_cost},
        "prompts": prompt_shas,
        # relative to the root, so the root names no directory of the machine that
        # planned it and a package can carry it unchanged
        "gate": {"config_sha256": gate_config_sha, "files": staged,
                 "out_of_tree": True,
                 "command": "python3 gate/verify_locate.py --gate-config gate/gate.json"},
        "locate": {
            "mode": mode, "status_rule": status_rule,
            "sourceset": {"id": None, "manifest_sha256": sha256_file(manifest)},
            "universe": {"id": universe_doc.get("id"),
                         "sha256": universe_doc.get("universe_sha256")},
            "inventory_run": inventory_run or None,
            "challenged_runs": [],
            "passages": planned_passages,
            "per_task": per_task,
            "source_ids": wanted,
        },
    }
    _write_json(out / "run.json", run_config)
    return planned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", default=vocab.MODE_LOCATE)
    parser.add_argument("--out", required=True, type=Path, help="the root to write")
    parser.add_argument("--inventory", required=True, type=Path,
                        help="the harvested inventory run's corpus directory")
    parser.add_argument("--inventory-run", default="", help="its run id")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path,
                        help="the sourceset manifest the run pins")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the sourceset's rendered sources")
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--index", type=Path, help="the declaration index")
    parser.add_argument("--prompts", type=Path, help="the prompts to stage")
    parser.add_argument("--sources-as", help="what gate.json names the sources directory, "
                                             "where that is not where this machine holds it "
                                             "(a package's plan names ../sources)")
    parser.add_argument("--candidates-as")
    parser.add_argument("--index-as")
    parser.add_argument("--per-task", type=int, default=PER_TASK)
    parser.add_argument("--model", default="")
    parser.add_argument("--max-cost", type=float, default=0.0)
    parser.add_argument("--source", action="append", default=[], dest="source_ids")
    args = parser.parse_args(argv)
    try:
        planned = plan(run_id=args.run_id, mode=args.mode, out=args.out,
                       inventory=args.inventory, universe=args.universe,
                       manifest=args.manifest, sources=args.sources,
                       candidates=args.candidates, index=args.index,
                       per_task=args.per_task, inventory_run=args.inventory_run,
                       model=args.model, max_cost=args.max_cost,
                       source_ids=args.source_ids, prompts=args.prompts,
                       sources_as=args.sources_as, candidates_as=args.candidates_as,
                       index_as=args.index_as)
    except (PlanProblem, corpus_mod.CorpusProblem, stage_mod.StageProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    passages = sum(len(d["passages"]) for t in planned["tasks"] for d in t["documents"])
    print(f"{args.run_id} [{args.mode}]: {len(planned['tasks'])} task(s), "
          f"{passages} passage(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
