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
    <root>/work/<task-id>/         the worker's whole world: its manifest, the article's
                                   rendered views, the candidates, the declaration
                                   search, and findings/<task-id>/ to write into
    <root>/findings/<task-id>/     what the gate passed, promoted out of work/
    <root>/usage/                  the agents' envelopes, which the watchdog reads
    <root>/gate-log/               the gate's own log, which the watchdog reads
    <root>/attempts/<task-id>.json what the runner did, attempt by attempt

**A worker reads only its own working directory**, because that is all
``site/agent_sandbox.py`` lets it reach, so everything a prompt names is staged there
before the call (``run/workdir.py``). The gate is **not** staged there: it runs out of
tree, and the *runner* runs it after the worker and before the reviewer
(``run/task.py``). A worker has no shell and is told to run nothing.

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
import workdir as workdir_mod  # noqa: E402 — scripts/pipeline/run/workdir.py

PLAN_SCHEMA = "wikilean.pipeline.plan/1"
RUN_SCHEMA = "wikilean.pipeline.run/1"
GATE_SCHEMA = "wikilean-gate-locate/1"
#: How many passages one task is handed by default.
PER_TASK = 8
#: The directories a root always has, so a runner never creates one by accident.
ROOT_DIRS = ("tasks", "work", "usage", "gate-log", "prompts")
#: What a locate task's manifest says of a passage: the frozen inventory's answer, and
#: nothing it decided for itself. A worker is handed the cut, not the inventory
#: document — ``scope`` (every planned passage is ``formal_math``), ``finder``,
#: ``concept`` and the rest are the cut's own bookkeeping and would only invite a
#: worker to re-open it.
LOCATE_PASSAGE_KEYS = ("item", "head_words", "span", "text_span", "quote", "section",
                       "passage_kind", "prominence", "statement", "conjuncts", "shape")
#: What a task manifest says of a curated candidate: the declaration and nothing else.
#: Why the retrieval curated it is in the candidates file the worker also reads; in the
#: manifest it is the list the gate holds the worker to answering.
CURATED_KEYS = ("library", "declaration")


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
    """``{source_id: entry}`` of a sourceset manifest **snapshot**.

    A sourceset version may be a *delta*, which holds only what changed since the
    version before it (:mod:`sourceset`); reading one here would silently plan against
    a handful of entries, so a delta is refused and the caller hands in the resolved
    entries instead (``entries=`` of :func:`plan`, which :mod:`cut` and :mod:`locate`
    pass from ``Sourceset.latest()``).
    """
    out: dict[str, dict] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            continue
        if number == 0 and str(entry.get("schema") or "").endswith("-delta/1"):
            raise PlanProblem(
                f"{path} is a delta version of a sourceset, which holds only the "
                f"entries that changed; plan through scripts/pipeline/cut.py or "
                f"locate.py, which resolve the chain first")
        if entry.get("source_id"):
            out[str(entry["source_id"])] = entry
    return out


def candidates_of(directory: Path | None, source_id: str) -> list[dict]:
    """The curated hits the retrieval left for a source, as a manifest names them."""
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
    seen, out = set(), []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        hit = {key: row.get(key) for key in CURATED_KEYS}
        key = tuple(hit.values())
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def task_passage(passage: dict) -> dict:
    """One passage as a locate task's manifest states it (:data:`LOCATE_PASSAGE_KEYS`)."""
    return {key: passage[key] for key in LOCATE_PASSAGE_KEYS if key in passage}


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


def _sources_of(manifest_doc: dict) -> list[str]:
    """Every source id a task manifest's documents name."""
    return [str(doc.get("source_id")) for doc in manifest_doc.get("documents") or []
            if doc.get("source_id")]


def task_id(run_id: str, source_id: str, number: int) -> str:
    """``<run>-<source>-<n>``: one task, named so a document's path says which it is."""
    return f"{run_id}-{source_id}-{number}"


def plan(*, run_id: str, mode: str, out: Path, universe: Path, manifest: Path,
         sources: Path, inventory: Path | None = None,
         task_documents: list[dict] | None = None, entries: dict | None = None,
         inventory_passages: dict | None = None,
         candidates: Path | None = None,
         index: Path | None = None, search: Path | None = None,
         per_task: int = PER_TASK,
         inventory_run: str = "", statement_set: dict | None = None,
         status_rule: int = vocab.STATUS_RULE_BROAD,
         span_end_rule: int = 3, statement_cut_rule: int = 4, judgment_rules: int = 5,
         model: str = "", max_cost: float = 0.0, source_ids: Iterable[str] = (),
         prompts: Path | None = None, render_prompts: bool = False,
         sources_as: str | None = None, candidates_as: str | None = None,
         index_as: str | None = None) -> dict:
    """Write the root and return its ``planned.json``.

    ``task_documents`` is what a mode module (stage 9b's :mod:`cut`, 9c's :mod:`locate`,
    9d's :mod:`challenge`) hands in when the task cut is its own; with none, the locate
    cut below is used. Either way this function owns the root: the manifests, the staged
    gate, the prompts and ``run.json``.

    ``statement_set`` is ``{set_id, seal_sha256}`` when the cut came from a sealed
    ``wikilean.statement-set/1`` rather than a harvested run (docs/STATEMENTS.md); it is
    recorded in ``run.json`` and changes nothing else; the task manifests of a run planned
    from a published cut are the manifests of a run planned from the cut itself but for
    the quote, which a statement set seals as the format's capped head
    (docs/STATEMENTS.md section 2).

    ``sources_as``, ``candidates_as`` and ``index_as`` are what ``gate.json`` **names**,
    where that is not where this machine holds them: a package carries the plan and no
    article text, so a coordinator plans against its own rendered sources and the plan
    names ``../sources``, which the contributor fills by refetching the pinned
    revisions. The view shas are still read from the real directory, so the plan pins
    the bytes the coordinator saw and the contributor's gate refuses anything else.
    """
    if mode not in vocab.MODES:
        raise PlanProblem(f"a batch is planned in one of {sorted(vocab.MODES)}, "
                          f"not {mode!r}")
    out = Path(out)
    if out.exists() and not out.is_dir():
        raise PlanProblem(f"{out} is not a directory")
    universe_doc = json.loads(Path(universe).read_text(encoding="utf-8"))
    entries = dict(entries) if entries is not None else read_manifest(manifest)
    if task_documents is None:
        if inventory is None and inventory_passages is None:
            raise PlanProblem(f"a {mode} batch is cut from a frozen inventory: name it "
                              f"with --inventory, or hand in the task documents")
        frozen = (inventory_passages if inventory_passages is not None
                  else frozen_inventory(inventory))
        wanted = sorted(set(source_ids) or set(frozen))
        missing = [sid for sid in wanted if sid not in frozen]
        if missing:
            raise PlanProblem(f"the inventory holds no document for "
                              f"{', '.join(missing[:5])}")
    else:
        frozen = {}
        wanted = sorted(set(source_ids) or {sid for doc in task_documents
                                            for sid in _sources_of(doc)})
    unpinned = [sid for sid in wanted if sid not in entries]
    if unpinned:
        raise PlanProblem(f"the sourceset manifest holds no entry for "
                          f"{', '.join(unpinned[:5])}")

    for name in ROOT_DIRS:
        (out / name).mkdir(parents=True, exist_ok=True)
    tasks: list[dict] = []
    planned_passages = 0
    if task_documents is not None:
        for manifest_doc in task_documents:
            tid = str(manifest_doc.get("task_id") or "")
            if not tid:
                raise PlanProblem("a task document names its task_id")
            if manifest_doc.get("mode") != mode:
                raise PlanProblem(f"{tid} is a {manifest_doc.get('mode')!r} task in a "
                                  f"{mode} batch")
            tasks.append(manifest_doc)
            planned_passages += sum(len(doc.get("passages") or doc.get("records") or [])
                                    for doc in manifest_doc.get("documents") or [])
            _write_json(out / "tasks" / f"{tid}.json", manifest_doc)
            (out / "work" / tid).mkdir(parents=True, exist_ok=True)
    else:
        for sid in wanted:
            curated = candidates_of(candidates, sid)
            for number, group in enumerate(cut_tasks(sid, frozen[sid], per_task=per_task,
                                                     mode=mode), 1):
                if not group:
                    continue
                tid = task_id(run_id, sid, number)
                entry = entries[sid]
                manifest_doc = {
                    "task_id": tid, "run_id": run_id, "mode": mode,
                    # a source split over several tasks hands its curated hits to the
                    # first of them: gate rule 4 makes a task answer for every one it
                    # is given, and two tasks owed the same answer owe it twice
                    "documents": [{"source_id": sid,
                                   "source_sha": entry.get("html_sha256"),
                                   "layer_sha256": entry.get("layer_sha256"),
                                   "renderer": entry.get("renderer"),
                                   "curated": curated if number == 1 else [],
                                   "passages": [task_passage(p) for p in group]}],
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
    gate_files = stage_mod.stage(out / "gate", scratch,
                                 sorted((out / "tasks").glob("*.json")))
    scratch.unlink()

    prompt_shas: dict[str, str] = {}
    if render_prompts:
        import prompts as prompts_mod  # noqa: PLC0415 — only a rendering run needs it
        # every slot is relative to the worker's own working directory, which
        # workdir.stage fills below: a worker reads nothing else and writes nowhere else
        prompts_mod.write(out / "prompts", mode, prompts_mod.slots(
            run_id=run_id, mode=mode,
            sources_dir=workdir_mod.SOURCES_DIR,
            findings_dir=workdir_mod.FINDINGS,
            candidates_dir=(workdir_mod.CANDIDATES_DIR if candidates else ""),
            index_dir=(workdir_mod.INDEX_DIR if search else ""),
            gate_verdict_file=workdir_mod.GATE_VERDICT,
            universe=str(universe_doc.get("id") or ""),
            universe_sha256=str(universe_doc.get("universe_sha256") or "")))
    if prompts is not None:
        for path in sorted(Path(prompts).glob("*.md")):
            target = out / "prompts" / path.name
            target.write_bytes(path.read_bytes())
    for path in sorted((out / "prompts").glob("*.md")):
        prompt_shas[path.name] = sha256_file(path)

    # Stage every task's working directory: the manifest, the article's rendered views,
    # the candidates and the declaration search, plus the one directory the worker may
    # write in. A prompt names these and nothing else, so staging is what makes the
    # prompt true — stage 9's prompts and stage 10's sandbox meet here and nowhere else.
    staged = {task["task_id"]: workdir_mod.stage(
        root=out, task_id=task["task_id"], sources=Path(sources),
        candidates=Path(candidates) if candidates else None,
        search=Path(search) if search else None)["files"] for task in tasks}

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
        "gate": {"config_sha256": gate_config_sha, "files": gate_files,
                 "out_of_tree": True,
                 "run_by": "the runner, after the worker and before the reviewer "
                           "(scripts/pipeline/run/task.py); never by the worker, which "
                           "has no shell",
                 "command": "python3 gate/verify_locate.py --gate-config gate/gate.json"},
        # what each task's working directory was staged with: the free pre-flight check
        # of run/workdir.py reads this beside the rendered prompt
        "work": {"staged": {task: len(files) for task, files in sorted(staged.items())},
                 "findings_dir": workdir_mod.FINDINGS,
                 "search": bool(search), "candidates": bool(candidates)},
        "locate": {
            "mode": mode, "status_rule": status_rule,
            "sourceset": {"id": None, "manifest_sha256": sha256_file(manifest)},
            "universe": {"id": universe_doc.get("id"),
                         "sha256": universe_doc.get("universe_sha256")},
            "inventory_run": inventory_run or None,
            "statement_set": dict(statement_set) if statement_set else None,
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
    parser.add_argument("--inventory", type=Path,
                        help="the harvested inventory run's corpus directory")
    parser.add_argument("--inventory-run", default="", help="its run id")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path,
                        help="the sourceset manifest the run pins")
    parser.add_argument("--sourceset", type=Path,
                        help="the sourceset directory, when --manifest names a delta "
                             "version: a delta holds only what changed, so the chain "
                             "is resolved here rather than planned against a handful "
                             "of entries")
    parser.add_argument("--sourceset-id")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the sourceset's rendered sources")
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--index", type=Path, help="the declaration index")
    parser.add_argument("--search", type=Path,
                        help="the worker's read-only declaration search corpus "
                             "(index_from_corpus.py --search), staged into each task")
    parser.add_argument("--prompts", type=Path, help="extra prompt files to stage")
    parser.add_argument("--task-documents", type=Path,
                        help="a directory of task manifests this batch's mode module "
                             "already cut (stage 9d's challenge.py writes them into "
                             "<out>/tasks); with it the locate cut below is not used")
    parser.add_argument("--render-prompts", action="store_true",
                        help="render this pipeline's own prompts into the root")
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
    entries = None
    if args.sourceset is not None:
        sys.path.insert(0, str(PIPELINE))
        import sourceset as sourceset_mod  # noqa: PLC0415

        ss = sourceset_mod.Sourceset.open(args.sourceset, args.sourceset_id)
        entries = ss.latest().entries
    documents = None
    if args.task_documents is not None:
        documents = []
        for path in sorted(Path(args.task_documents).glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                documents.append(value)
        if not documents:
            print(f"no task manifest in {args.task_documents}", file=sys.stderr)
            return 2
    try:
        planned = plan(run_id=args.run_id, mode=args.mode, out=args.out,
                       task_documents=documents, entries=entries,
                       inventory=args.inventory, universe=args.universe,
                       manifest=args.manifest, sources=args.sources,
                       candidates=args.candidates, index=args.index,
                       search=args.search,
                       per_task=args.per_task, inventory_run=args.inventory_run,
                       render_prompts=args.render_prompts,
                       model=args.model, max_cost=args.max_cost,
                       source_ids=args.source_ids, prompts=args.prompts,
                       sources_as=args.sources_as, candidates_as=args.candidates_as,
                       index_as=args.index_as)
    except (PlanProblem, corpus_mod.CorpusProblem, stage_mod.StageProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # a challenge task's documents hold records, not passages
    items = sum(len(d.get("passages") or d.get("records") or [])
                for t in planned["tasks"] for d in t["documents"])
    what = "record" if args.mode == vocab.MODE_CHALLENGE else "passage"
    print(f"{args.run_id} [{args.mode}]: {len(planned['tasks'])} task(s), "
          f"{items} {what}(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
