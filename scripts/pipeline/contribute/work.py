#!/usr/bin/env python3
"""Contribute mode's middle: materialise a package, run it, gate it, harvest it.

docs/PIPELINE.md section 3 (stages 10 and 11). Between ``--stage claim`` and
``--stage results`` a contributor used to do everything by hand. This is that part, as
far as it can go without a model run:

1. **materialise** — lay the package's plan out as a run root under the contributor's
   working directory, and name the three directories the contributor fills: the rendered
   sources, the candidates and the declaration index. The package carries **no article
   text**, so the sources are the contributor's own refetch of the pinned revisions; the
   plan already pins their shas, and the gate refuses anything else (stage 7, rule 2).
   Nothing is rewritten here: a planned root names everything inside it relatively, so
   it moves to another machine unchanged.
2. **work** — one agent call per task, through :mod:`run.agent`, which goes through
   ``site/agent_sandbox.py``. A call is a dry run unless ``--execute``, and
   ``--worker-command`` runs a local command in its place, which is what a stub worker
   and the tests use.
3. **gate** — the staged gate over each task's own documents, by absolute path, out of
   the worktree the worker wrote in. A task the gate rejects is named and its documents
   are not harvested.
4. **harvest** — the reconciliation rule of stage 10 over the gated documents, into
   ``<workdir>/corpus/<run-id>/`` with a ``RECEIPT.json``. That directory and the run's
   own records are what ``--stage results`` sends back.

**The spend cap is recorded, not enforced.** No watchdog runs on a contributor machine:
the package states the cap, this prints it, and a person watches the cost. The watchdog
is the coordinator's, on a root it owns.

What is still TODO: the refetch and the render (stage 9a), the candidates (9c) and the
real worker prompts (9b–d). A contributor supplies the three directories by hand until
those land; :func:`missing_inputs` says which are not there yet, by name.
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

import agent as agent_mod  # noqa: E402
import harvest as harvest_mod  # noqa: E402
import package as package_mod  # noqa: E402
import verify_locate as gate_mod  # noqa: E402

#: Where the run root goes under the contributor's working directory.
ROOT_DIR = "root"
#: Where the harvested documents go, which ``--stage results`` sends back.
CORPUS_DIR = "corpus"
#: The directories a contributor fills before it can run, created inside the root where
#: the plan names them: the package carries none of them, because each is either article
#: text or a map of a Lean library.
INPUT_DIRS = ("sources", "candidates", "index")
#: Files of the package that are not part of the plan and do not travel into the root.
NOT_THE_PLAN = ("package.json", "sources.jsonl", "universe.json")


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
    for name in (*INPUT_DIRS, "usage", "gate-log", "work", "prompts"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def gate_config(root: Path) -> dict:
    return json.loads((Path(root) / "gate" / "gate.json").read_text(encoding="utf-8"))


def missing_inputs(root: Path) -> list[str]:
    """What the plan names and this machine does not hold yet, by name.

    Said before anything runs, because a worker that cannot read its source writes a
    document the gate refuses, and a contributor should learn that from one line rather
    than from a task's worth of model time.
    """
    config = gate_config(root)
    gate_dir = Path(root) / "gate"

    def resolve(value) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_absolute() else gate_dir / path

    out: list[str] = []
    sources = resolve(config.get("sources_dir"))
    for source_id in sorted(config.get("sources") or {}):
        if sources is None or not (sources / source_id / "layer.txt").is_file():
            out.append(f"the rendered source {source_id} (layer.txt, formulas.json, "
                       "paragraphs.json)")
    candidates = resolve(config.get("candidates_dir"))
    if candidates is not None and not candidates.is_dir():
        out.append(f"the candidates directory the plan names ({config['candidates_dir']})")
    index = resolve((config.get("index") or {}).get("path"))
    if index is not None and not index.is_file():
        out.append(f"the declaration index the plan names ({config['index']['path']})")
    return out


def tasks_of(root: Path) -> list[str]:
    """Every task of the planned root, in order."""
    directory = Path(root) / "tasks"
    return sorted(path.stem for path in directory.glob("*.json")) if directory.is_dir() else []


def work(root: Path, *, tasks: Iterable[str] | None = None, worker_command: str = "",
         execute: bool = False, model: str | None = None, prompt: str = "",
         log=print) -> list[dict]:
    """One call per task; returns the envelopes. Nothing is spent without ``--execute``."""
    root = Path(root)
    out = []
    for task in (list(tasks) if tasks is not None else tasks_of(root)):
        body = prompt or _prompt_for(root, task)
        envelope = agent_mod.call(root=root, task_id=task, role="worker", prompt=body,
                                  model=model, worker_command=worker_command,
                                  execute=execute)
        state = ("error" if envelope.get("is_error") else
                 "dry run" if envelope.get("dry_run") else "done")
        log(f"  {task}: {state}")
        out.append(envelope)
    return out


def _prompt_for(root: Path, task: str) -> str:
    """The worker's prompt: the staged one with the task named, else a one-line brief.

    The real prompts are stage 9's and are staged into the root by the planner; a root
    planned without them gets a brief that names the task's manifest, so a stub worker
    and a dry run still have something to be handed.
    """
    staged = Path(root) / "prompts" / "worker.md"
    body = staged.read_text(encoding="utf-8") if staged.is_file() else (
        "Locate every passage of your task's manifest. Write your document to "
        "findings/{{TASK_ID}}/<source-id>.json.")
    return body.replace("{{TASK_ID}}", task)


def gate(root: Path, task: str, *, log=print) -> int:
    """Run the staged gate over one task's own documents; return its exit code."""
    root = Path(root)
    worktree = root / "work" / task
    findings = worktree / "findings"
    code = gate_mod.main(["--gate-config", str((root / "gate" / "gate.json").resolve()),
                          "--root", str(worktree), "--findings-dir", str(findings)],
                         env={"TEND_TASK_ID": task})
    log(f"  {task}: gate exit {code}")
    return code


def gate_all(root: Path, *, tasks: Iterable[str] | None = None,
             log=print) -> tuple[list[str], list[str]]:
    """``(passed, rejected)``: the gate over every task that wrote a document."""
    root = Path(root)
    passed, rejected = [], []
    for task in (list(tasks) if tasks is not None else tasks_of(root)):
        findings = root / "work" / task / "findings"
        if not any(findings.glob("*/*.json")) if findings.is_dir() else True:
            log(f"  {task}: no document")
            continue
        (passed if gate(root, task, log=log) == gate_mod.EXIT_OK else rejected).append(task)
    return passed, rejected


def collect(root: Path, tasks: Iterable[str], into: Path) -> Path:
    """Put the gated documents of ``tasks`` under one directory, as the harvest's source."""
    into = Path(into)
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    for task in tasks:
        findings = Path(root) / "work" / task / "findings"
        for path in sorted(findings.glob("*/*.json")):
            target = into / path.relative_to(findings)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return into


def harvest(root: Path, workdir: Path, run_id: str, tasks: Iterable[str]) -> dict:
    """Reconcile the gated documents into ``<workdir>/corpus/<run-id>`` with a receipt."""
    root, workdir = Path(root), Path(workdir)
    staged = collect(root, tasks, workdir / "gated")
    config = None
    run_json = root / "run.json"
    if run_json.is_file():
        config = json.loads(run_json.read_text(encoding="utf-8"))
    return harvest_mod.harvest(run_id=run_id, source=staged,
                               dest=workdir / CORPUS_DIR / run_id,
                               usage=root / "usage", gate_log=root / "gate-log",
                               run_config=config)


def run_middle(*, package_dir: Path, workdir: Path, run_id: str, worker_command: str = "",
               execute: bool = False, model: str | None = None,
               max_cost: float | None = None, log=print) -> dict:
    """materialise, work, gate, harvest — and say what came of each.

    Returns ``{"root", "tasks", "passed", "rejected", "receipt", "missing_inputs"}``.
    """
    root = materialise(package_dir, workdir)
    log(f"  root     : {root}")
    missing = missing_inputs(root)
    for line in missing:
        log(f"  missing  : {line}")
    if max_cost is not None:
        log(f"  spend cap: USD {max_cost} — recorded, not enforced here: no watchdog runs "
            "on a contributor machine")
    tasks = tasks_of(root)
    log(f"  tasks    : {len(tasks)}")
    work(root, tasks=tasks, worker_command=worker_command, execute=execute, model=model,
         log=log)
    passed, rejected = gate_all(root, tasks=tasks, log=log)
    receipt = harvest(root, workdir, run_id, passed)
    log(harvest_mod.describe(receipt))
    if rejected:
        log(f"  rejected : {', '.join(rejected)} — their documents are not harvested")
    return {"root": root, "tasks": tasks, "passed": passed, "rejected": rejected,
            "receipt": receipt, "missing_inputs": missing}
