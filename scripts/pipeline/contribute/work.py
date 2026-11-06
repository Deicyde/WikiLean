#!/usr/bin/env python3
"""Contribute mode's middle: materialise a package, stage it, run it, gate it, harvest it.

docs/PIPELINE.md section 3 (stages 10 and 11). Between ``--stage claim`` and
``--stage results`` a contributor used to do everything by hand. This is that part, and
since the stage-9/stage-10 join landed it is **the same code the coordinator runs**:

1. **materialise** — lay the package's plan out as a run root under the contributor's
   working directory, and name the directories the contributor fills: the rendered
   sources, the candidates, the declaration index and the search corpus. The package
   carries **no article text**, so the sources are the contributor's own refetch of the
   pinned revisions; the plan already pins their shas, and the gate refuses anything
   else (stage 7, rule 2). Nothing is rewritten here: a planned root names everything
   inside it relatively, so it moves to another machine unchanged.
2. **stage** — ``run/workdir.py`` fills ``work/<task-id>/`` with that task's manifest,
   its article's rendered views, its candidates and the declaration search, plus the
   one directory the worker may write in, and writes ``STAGED.json``. **The coordinator
   does not send its own staged directories** (``contribute/package.py``'s
   ``NOT_PACKAGED``): they hold the coordinator's copy of the article text, so a
   contributor stages its own, after refetching, from its own bytes. That is the "no
   article text ever leaves your machine" promise, enforced rather than asserted.
3. **run, gate, retry, promote** — ``run/task.py``, once per task: the worker call
   through ``site/agent_sandbox.py``, the mechanical passage ids, **the runner's own
   gate call** out of tree, one retry that hands the gate's refusal list back, and the
   promotion of a gated document to ``<root>/findings/<task-id>/``. The worker never
   runs the gate and never could: it has no shell.
4. **harvest** — the reconciliation rule of stage 10 over the promoted documents, into
   ``<workdir>/corpus/<run-id>/`` with a ``RECEIPT.json``. That directory and the run's
   own records are what ``--stage results`` sends back.

**It is a dry run until ``--execute``.** A dry run stages everything, calls no model,
writes no document, and still says which inputs are missing and what the run would do —
so a contributor sees the shape of the run, and its task count, before paying for one.
:func:`check_prompts` is the other free step: it reads the rendered prompts, pulls every
path out of them and asks whether staging put each one there. Run it before any paid
call; it is the check the run of 2026-11-06 stopped for want of.

**Nothing is run when an input is missing.** A worker that cannot read its source writes
a document the gate refuses, so :func:`missing_inputs` names what is not there yet and
the middle harvests nothing rather than spending a task's worth of model time on it.

**The spend cap is recorded, not enforced.** No watchdog runs on a contributor machine:
the package states the cap, this prints it, and a person watches the cost. The watchdog
is the coordinator's, on a root it owns.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE, PIPELINE / "gate", PIPELINE / "run"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import harvest as harvest_mod  # noqa: E402
import package as package_mod  # noqa: E402
import task as task_mod  # noqa: E402
import workdir as workdir_mod  # noqa: E402

#: Where the run root goes under the contributor's working directory.
ROOT_DIR = "root"
#: Where the harvested documents go, which ``--stage results`` sends back.
CORPUS_DIR = "corpus"
#: The directories a contributor fills before it can run, created inside the root where
#: the plan names them: the package carries none of them, because each is either article
#: text or a map of a Lean library.
INPUT_DIRS = ("sources", "candidates", "index")
#: Where the search corpus goes, under ``index/``: ``index_from_corpus.py`` writes the
#: declaration index as one file and the search corpus as a directory, and the plan's
#: ``gate.json`` names the file, so the directory sits beside it under a name of its own.
SEARCH_DIR = "search"
#: Files of the package that are not part of the plan and do not travel into the root.
NOT_THE_PLAN = ("package.json", "sources.jsonl", "universe.json")
#: The directories the root needs that the plan does not carry.
MADE_DIRS = (*INPUT_DIRS, "usage", "gate-log", "work", "prompts", "attempts", "findings")


class WorkProblem(ValueError):
    """A package that cannot be run here, named rather than run anyway."""


def materialise(package_dir: Path, workdir: Path, *, replace: bool = False) -> Path:
    """Copy the package's plan into ``<workdir>/root`` and return the root.

    Every file but the manifest, the source pins and the universe — those are the
    package's own description and the plan does not read them from inside itself. The
    package's shas are checked first by :func:`package.read`, so what lands here is what
    the coordinator published.

    **It is re-runnable.** The plan's files are written over whatever is there, and the
    directories the contributor fills — the sources, the candidates, the index — and
    everything the run has produced are left alone: a second ``--stage run``, which is
    the normal way to carry on after filling an input, must not throw away the fetch.
    ``replace`` empties the root first, for a contributor starting again; a root that
    holds another package's plan is refused either way.
    """
    package_dir, workdir = Path(package_dir), Path(workdir)
    manifest = package_mod.read(package_dir)
    root = workdir / ROOT_DIR
    if root.exists() and replace:
        shutil.rmtree(root)
    here = root / "run.json"
    if here.is_file():
        try:
            held = json.loads(here.read_text(encoding="utf-8")).get("run_id")
        except ValueError:
            held = None
        if held and held != manifest.get("run_id"):
            raise WorkProblem(f"{root} holds run {held!r} and this package is "
                              f"{manifest.get('run_id')!r}; use another --workdir, or pass "
                              "--replace to start it again")
    root.mkdir(parents=True, exist_ok=True)
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(package_dir)
        if rel.as_posix() in NOT_THE_PLAN or rel.parts[0] == package_mod.INPUTS:
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    if not (root / "gate" / "gate.json").is_file():
        raise WorkProblem(f"{package_dir} carries no planned gate (gate/gate.json); it is "
                          "not a package this pipeline planned")
    for name in MADE_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def gate_config(root: Path) -> dict:
    return json.loads((Path(root) / "gate" / "gate.json").read_text(encoding="utf-8"))


def run_config(root: Path) -> dict:
    """The plan's ``run.json``, or an empty mapping for a root that carries none."""
    path = Path(root) / "run.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def hands_out(root: Path) -> tuple[bool, bool]:
    """``(candidates, search)``: which of the two optional inputs this run uses.

    The planner records both in ``run.json``'s ``work`` block, which travels in the
    package. A root planned before that block existed falls back to what ``gate.json``
    names, so an older package still says something true.
    """
    block = (run_config(root).get("work") or {})
    if "candidates" in block or "search" in block:
        return bool(block.get("candidates")), bool(block.get("search"))
    config = gate_config(root)
    return bool(config.get("candidates_dir")), bool(config.get("index"))


def sources_dir(root: Path) -> Path:
    return Path(root) / INPUT_DIRS[0]


def candidates_dir(root: Path) -> Path:
    return Path(root) / INPUT_DIRS[1]


def search_dir(root: Path) -> Path:
    return Path(root) / INPUT_DIRS[2] / SEARCH_DIR


def tasks_of(root: Path) -> list[str]:
    """Every task of the planned root, in order."""
    directory = Path(root) / "tasks"
    return sorted(path.stem for path in directory.glob("*.json")) if directory.is_dir() else []


def missing_inputs(root: Path) -> list[str]:
    """What the plan names and this machine does not hold yet, by name.

    Said before anything runs, because a worker that cannot read its source writes a
    document the gate refuses, and a contributor should learn that from one line rather
    than from a task's worth of model time. Every name here is a file **staging** needs
    (``run/workdir.py``), plus the declaration index the *gate* joins from, which is not
    staged into a worker's directory at all.
    """
    root = Path(root)
    config = gate_config(root)
    gate_dir = root / "gate"
    use_candidates, use_search = hands_out(root)
    out: list[str] = []

    wanted: list[str] = []
    for task in tasks_of(root):
        try:
            manifest = workdir_mod.task_manifest(root, task)
        except workdir_mod.WorkdirProblem:
            continue
        for sid in workdir_mod.sources_of(manifest):
            if sid not in wanted:
                wanted.append(sid)
    for sid in wanted or sorted(config.get("sources") or {}):
        absent = [name for name in workdir_mod.WORKER_VIEWS
                  if not (sources_dir(root) / sid / name).is_file()]
        if absent:
            out.append(f"the rendered source {sid}: {', '.join(absent)} — refetch "
                       f"revision {sid} at its pin and render it into "
                       f"{INPUT_DIRS[0]}/{sid}/")
        if use_candidates and not (candidates_dir(root) / f"{sid}.json").is_file():
            out.append(f"the candidates for {sid} ({INPUT_DIRS[1]}/{sid}.json)")
    if use_search:
        absent = [name for name in workdir_mod.SEARCH_FILES
                  if not (search_dir(root) / name).is_file()]
        if absent:
            out.append(f"the declaration search corpus: {', '.join(absent)} — "
                       f"index_from_corpus.py --search writes them into "
                       f"{INPUT_DIRS[2]}/{SEARCH_DIR}/")

    index = (config.get("index") or {}).get("path")
    if index:
        path = Path(str(index))
        if not (path if path.is_absolute() else gate_dir / path).is_file():
            out.append(f"the declaration index the gate joins from ({index})")
    return out


# ---- staging, and the free check over it ------------------------------------------------


def stage_tasks(root: Path, *, tasks: Iterable[str] | None = None,
                log=print) -> dict[str, dict]:
    """Stage every task's working directory from **this machine's** inputs.

    ``run/workdir.py`` is the one place that says what staging is, and this calls it
    rather than copying its rules: the manifest, the article's rendered views by name,
    the candidates and the search corpus, and ``findings/<task-id>/`` as the only
    directory the worker may write in. Idempotent, and it never touches ``findings/``.
    """
    root = Path(root)
    use_candidates, use_search = hands_out(root)
    staged: dict[str, dict] = {}
    for task in (list(tasks) if tasks is not None else tasks_of(root)):
        record = workdir_mod.stage(
            root=root, task_id=task, sources=sources_dir(root),
            candidates=candidates_dir(root) if use_candidates else None,
            search=search_dir(root) if use_search else None)
        staged[task] = record
        log(f"  {task}: {len(record['files'])} file(s) staged")
    return staged


def check_prompts(root: Path, *, tasks: Iterable[str] | None = None,
                  log=print) -> dict[str, dict]:
    """The free pre-flight: is every path the prompts name in the staged directory?

    No model, no spend. A path a prompt names that staging did not put there is the
    defect that stops a paid run halfway, and it costs nothing to find here.
    """
    root = Path(root)
    prompts = sorted((root / "prompts").glob("*.md"))
    out: dict[str, dict] = {}
    for task in (list(tasks) if tasks is not None else tasks_of(root)):
        got = workdir_mod.check_prompt_names(root=root, task_id=task, prompts=prompts)
        out[task] = got
        if got["ok"]:
            log(f"  {task}: every path the prompts name is staged "
                f"({len(got['present'])})")
        else:
            log(f"  {task}: NOT STAGED — {', '.join(got['missing'])}")
    return out


# ---- the run ----------------------------------------------------------------------------


def run_tasks(root: Path, *, tasks: Iterable[str] | None = None,
              worker_command: str = "", execute: bool = False,
              model: str | None = None, reviewer: bool = True,
              max_attempts: int = task_mod.MAX_ATTEMPTS, log=print) -> list[dict]:
    """One ``run/task.py`` task per task: call, gate, retry, promote. Returns the records."""
    root = Path(root)
    if not (root / "prompts" / "worker.md").is_file():
        raise WorkProblem(
            f"{root}/prompts/worker.md is not there, so there is no prompt to hand a "
            "worker. A published package carries stage 9's rendered prompts; a root "
            "planned without them cannot be run, only inspected")
    use_candidates, use_search = hands_out(root)
    out = []
    for task in (list(tasks) if tasks is not None else tasks_of(root)):
        record = task_mod.run_task(
            root=root, task_id=task, sources=sources_dir(root),
            candidates=candidates_dir(root) if use_candidates else None,
            search=search_dir(root) if use_search else None,
            model=model, execute=execute, worker_command=worker_command,
            reviewer=reviewer, max_attempts=max_attempts)
        log("  " + task_mod.describe(record))
        out.append(record)
    return out


def collect(root: Path, tasks: Iterable[str], into: Path) -> Path:
    """Put the gated documents of ``tasks`` under one directory, as the harvest's source.

    ``run/task.py`` promotes a document the gate passed to ``<root>/findings/<task-id>/``
    and leaves a refused one in the working directory, so this reads the promoted ones
    and the corpus holds gated documents or it is not a corpus.
    """
    into = Path(into)
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    for task in tasks:
        promoted = Path(root) / workdir_mod.FINDINGS / task
        for path in sorted(promoted.glob("*.json")):
            target = into / task / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return into


def harvest(root: Path, workdir: Path, run_id: str, tasks: Iterable[str]) -> dict:
    """Reconcile the gated documents into ``<workdir>/corpus/<run-id>`` with a receipt.

    The gate log is the **file** ``verify_locate.py`` appends to,
    ``gate-log/<run-id>.jsonl``, not the directory it sits in: a receipt built over the
    directory records no gate at all, and a corpus whose receipt records no gate is one
    the tally refuses to seal (``corpus.require_gated``).
    """
    root, workdir = Path(root), Path(workdir)
    staged = collect(root, tasks, workdir / "gated")
    config = None
    run_json = root / "run.json"
    if run_json.is_file():
        config = json.loads(run_json.read_text(encoding="utf-8"))
    return harvest_mod.harvest(run_id=run_id, source=staged,
                               dest=workdir / CORPUS_DIR / run_id,
                               usage=root / "usage",
                               gate_log=root / "gate-log" / f"{run_id}.jsonl",
                               run_config=config)


def run_middle(*, package_dir: Path, workdir: Path, run_id: str, worker_command: str = "",
               execute: bool = False, model: str | None = None,
               max_cost: float | None = None, reviewer: bool = True,
               max_attempts: int = task_mod.MAX_ATTEMPTS, log=print) -> dict:
    """materialise, stage, check, run, harvest — and say what came of each.

    Returns ``{"root", "tasks", "staged", "prompts", "records", "passed", "rejected",
    "receipt", "missing_inputs"}``. A root with a missing input stages nothing and runs
    nothing: the receipt is empty and the missing list is the answer.
    """
    root = materialise(package_dir, workdir)
    log(f"  root     : {root}")
    if not execute and not worker_command:
        log("  DRY RUN  : no model will be called and no document written. This is the "
            "default; pass --execute when you have read what it would do.")
    missing = missing_inputs(root)
    for line in missing:
        log(f"  missing  : {line}")
    if max_cost is not None:
        log(f"  spend cap: USD {max_cost} — recorded, not enforced here: no watchdog runs "
            "on a contributor machine")
    tasks = tasks_of(root)
    log(f"  tasks    : {len(tasks)}")
    staged: dict[str, dict] = {}
    prompts: dict[str, dict] = {}
    records: list[dict] = []
    passed: list[str] = []
    rejected: list[str] = []
    if missing:
        log("  stopped  : an input the plan names is not on this machine, so nothing "
            "was staged and nothing was run. Fill the names above and run this again.")
    else:
        staged = stage_tasks(root, tasks=tasks, log=log)
        prompts = check_prompts(root, tasks=tasks, log=log)
        unready = sorted(task for task, got in prompts.items() if not got["ok"])
        if unready:
            log(f"  stopped  : {', '.join(unready)} — a prompt names a path staging did "
                "not put there, and a paid call would read a file that is not there. "
                "This check is free; the call is not.")
        else:
            records = run_tasks(root, tasks=tasks, worker_command=worker_command,
                                execute=execute, model=model, reviewer=reviewer,
                                max_attempts=max_attempts, log=log)
            passed = [r["task_id"] for r in records if (r.get("gate") or {}).get("ok")]
            rejected = [r["task_id"] for r in records
                        if not (r.get("gate") or {}).get("ok")]
            outside = [r["task_id"] for r in records if r.get("wrote_outside")]
            if outside:
                log(f"  WROTE OUTSIDE: {', '.join(outside)} — a worker left a file "
                    "outside its findings directory; read attempts/<task>.json before "
                    "sending anything")
    receipt = harvest(root, workdir, run_id, passed)
    log(harvest_mod.describe(receipt))
    if rejected:
        log(f"  rejected : {', '.join(rejected)} — their documents are not harvested")
    return {"root": root, "tasks": tasks, "staged": staged, "prompts": prompts,
            "records": records, "passed": passed, "rejected": rejected,
            "receipt": receipt, "missing_inputs": missing}
