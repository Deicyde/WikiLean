#!/usr/bin/env python3
"""Stage 10: run one task — stage, call, key, gate, retry, review, promote.

docs/PIPELINE.md section 3 (stage 10). Stage 9 writes the prompts and stage 10 makes
the call, and until 2026-11-06 nothing joined the two: the prompts described a worker
sitting in the run root with a shell, and the sandbox gives a worker one directory,
three read tools and no shell at all. **This module is the join.** Everything a prompt
names, the runner stages; everything a prompt used to tell the worker to *run*, the
runner runs.

One task, in order:

1. **stage** — ``workdir.stage`` fills ``work/<task-id>/`` with the manifest, the
   article's rendered views, the candidates and the declaration search, and makes the
   one directory the worker may write in;
2. **the worker call** — through :mod:`agent`, with ``site/agent_sandbox``'s options:
   Read, Grep, Glob, and a Write that reaches ``findings/<task-id>/`` and nowhere else;
3. **the ids** — for an inventory, ``gate/passage_ids.py`` puts each quote on the layer
   and fills ``item``, ``head_words`` and ``span``. Mechanical, and never the model's;
4. **the gate** — ``gate/verify_locate.py``, out of tree, by absolute path, as the
   run's own validation call so the watchdog's gate-failure rule and the harvest's
   receipt both see it. **The runner runs it. The worker never did and never could;**
5. **the retry** — an exit 1 is handed back to the worker as feedback, once per
   ``--max-attempts``. The plan's attempt rules are the caller's: ``--max-attempts 2``
   is one retry, and a task still refused after the last attempt is left unharvested
   and recorded, which is stop rule S7's "carry on";
6. **the reviewer** — form only, reading the documents and the gate's own output, which
   the runner leaves in the working directory for it;
7. **the promotion** — a document the gate passed is copied to
   ``<root>/findings/<task-id>/``, which is what the harvest reads. A document the gate
   refused is left where it is: the corpus holds gated documents or it is not a corpus.

``attempts/<task-id>.json`` records every step with its exit, so a shard's history is
on disk and not in a terminal.

    python3 scripts/pipeline/run/task.py --root <root> --task <task-id> \\
        --sources <dir> [--candidates <dir>] [--search <dir>] \\
        [--model <name>] [--max-attempts 2] [--no-reviewer] \\
        [--execute | --worker-command '<cmd>' | --stub-worker '<cmd>']
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE, PIPELINE / "gate"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import agent as agent_mod  # noqa: E402
import vocab  # noqa: E402
import workdir as workdir_mod  # noqa: E402

#: The gate's exit vocabulary, as scripts/pipeline/prompts.py reads it back.
GATE_OK, GATE_REJECTED, GATE_BROKEN = 0, 1, 3
#: How many times a worker is called for one task by default: the first call and one
#: retry, which is what the signed plan's §2 budgets for.
MAX_ATTEMPTS = 2
#: What the feedback turn says, above the gate's own refusal list.
FEEDBACK_HEAD = """
---

## The gate refused your document; this is attempt {attempt} of {total}

The runner ran the gate over what you wrote, out of tree, against this run's own pins.
It refused the document with the problems below, each naming the rule it broke. **Fix
exactly these and write the document again**, to the same path. Change nothing the gate
did not name, and never weaken a judgement to make a refusal go away: an honest
`unresolved` outcome passes the gate and a wrong `located` does not stop being wrong.

```
{refusals}
```
"""


class TaskProblem(ValueError):
    """A task that cannot be run as asked, named rather than run another way."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_prompt(path: Path | None) -> str:
    return Path(path).read_text(encoding="utf-8") if path else ""


# ---- the runner's two mechanical steps --------------------------------------------


def run_passage_ids(root: Path, task_id: str) -> dict:
    """Key an inventory's passages from their quotes, before the gate sees them."""
    documents = workdir_mod.documents(root, task_id)
    if not documents:
        return {"ran": False, "why": "the worker left no document"}
    tool = Path(root) / "gate" / "passage_ids.py"
    if not tool.is_file():
        raise TaskProblem(f"the gate directory has no passage_ids.py at {tool}")
    argv = [sys.executable, str(tool), "--gate-config", str(root / "gate" / "gate.json"),
            "--write"]
    for path in documents:
        argv += ["--document", str(path)]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return {"ran": True, "exit": proc.returncode,
            "output": (proc.stdout + proc.stderr)[-4000:]}


def run_gate(root: Path, task_id: str) -> dict:
    """The gate, out of tree, over this task's own documents. The runner's call.

    It goes in as ``--changed-files``, which is the run's **validation** call: that is
    the record the spend watchdog's gate-failure rule counts and the harvest receipt
    reads, and it is what makes rule 7 — "the changed files are this task's own" —
    actually run.
    """
    work = Path(root) / "work" / task_id
    documents = workdir_mod.documents(root, task_id)
    listing = work / "changed-files.txt"
    listing.write_text("".join(
        f"{workdir_mod.FINDINGS}/{task_id}/{path.name}\n" for path in documents),
        encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(Path(root) / "gate" / "verify_locate.py"),
         "--gate-config", str(Path(root) / "gate" / "gate.json"),
         "--root", str(work), "--changed-files", str(listing),
         "--task-id", task_id],
        capture_output=True, text=True, check=False,
        env={**os.environ, "TEND_TASK_ID": task_id})
    output = (proc.stdout + proc.stderr).strip()
    (work / workdir_mod.GATE_VERDICT).write_text(
        f"$ verify_locate.py --task-id {task_id}\nexit {proc.returncode}\n\n{output}\n",
        encoding="utf-8")
    listing.unlink(missing_ok=True)
    return {"exit": proc.returncode, "ok": proc.returncode == GATE_OK,
            "documents": [path.name for path in documents], "output": output[-8000:]}


def promote(root: Path, task_id: str) -> list[str]:
    """Copy the gated documents to ``<root>/findings/<task-id>/``, what the harvest reads."""
    target = Path(root) / workdir_mod.FINDINGS / task_id
    target.mkdir(parents=True, exist_ok=True)
    out = []
    for path in workdir_mod.documents(root, task_id):
        shutil.copyfile(path, target / path.name)
        out.append(path.name)
    return out


# ---- the task ----------------------------------------------------------------------


def run_task(*, root: Path, task_id: str, sources: Path, candidates: Path | None = None,
             search: Path | None = None, prompts: Path | None = None,
             model: str | None = None, execute: bool = False,
             worker_command: str = "", reviewer: bool = True,
             max_attempts: int = MAX_ATTEMPTS, timeout: float = 5400.0) -> dict:
    """Run one task end to end and return its record; writes ``attempts/<task>.json``."""
    root = Path(root)
    if max_attempts < 1:
        raise TaskProblem(f"a task is attempted at least once, not {max_attempts} times")
    prompts = Path(prompts) if prompts else root / "prompts"
    mode = str(json.loads((root / "run.json").read_text(encoding="utf-8")).get("mode")
               or "") if (root / "run.json").is_file() else ""

    staged = workdir_mod.stage(root=root, task_id=task_id, sources=Path(sources),
                               candidates=Path(candidates) if candidates else None,
                               search=Path(search) if search else None)
    record: dict = {"schema": "wikilean.pipeline.attempts/1", "task_id": task_id,
                    "mode": mode, "started": _now(), "staged": len(staged["files"]),
                    "attempts": [], "gate": None, "reviewer": None,
                    "promoted": [], "wrote_outside": []}

    system = read_prompt(prompts / "worker-system.md")
    base = read_prompt(prompts / "worker.md")
    gate: dict = {"exit": None, "ok": False, "output": "", "documents": []}
    for attempt in range(1, max_attempts + 1):
        prompt = base if attempt == 1 else base + FEEDBACK_HEAD.format(
            attempt=attempt, total=max_attempts, refusals=gate.get("output") or "")
        envelope = agent_mod.call(root=root, task_id=task_id, role="worker",
                                  prompt=prompt, system=system, model=model,
                                  worker_command=worker_command, execute=execute,
                                  timeout=timeout, step=f"attempt{attempt}")
        step: dict = {"attempt": attempt, "at": _now(),
                      "cost_usd": envelope.get("total_cost_usd"),
                      "is_error": bool(envelope.get("is_error")),
                      "documents": [p.name for p in workdir_mod.documents(root, task_id)]}
        if not step["documents"]:
            step["gate"] = {"exit": None, "ok": False,
                            "output": "the worker wrote no document"}
            record["attempts"].append(step)
            gate = step["gate"]
            continue
        if mode == vocab.MODE_INVENTORY:
            step["ids"] = run_passage_ids(root, task_id)
        gate = run_gate(root, task_id)
        step["gate"] = {key: gate[key] for key in ("exit", "ok", "documents")}
        step["gate"]["output"] = gate["output"][-2000:]
        record["attempts"].append(step)
        if gate["ok"] or gate["exit"] == GATE_BROKEN:
            # a broken gate is an infrastructure fault, and a second paid call would
            # buy another document nobody can judge
            break
    record["gate"] = {key: gate.get(key) for key in ("exit", "ok")}
    record["gate"]["output"] = (gate.get("output") or "")[-8000:]

    if reviewer and workdir_mod.documents(root, task_id):
        envelope = agent_mod.call(
            root=root, task_id=task_id, role="reviewer",
            prompt=read_prompt(prompts / "reviewer.md"),
            system=read_prompt(prompts / "reviewer-system.md"), model=model,
            worker_command="", execute=execute, timeout=timeout)
        record["reviewer"] = {"cost_usd": envelope.get("total_cost_usd"),
                              "is_error": bool(envelope.get("is_error")),
                              "verdict_text": str(envelope.get("result") or "")[-4000:]}

    record["wrote_outside"] = workdir_mod.wrote_outside(root, task_id, staged)
    if record["gate"]["ok"]:
        record["promoted"] = promote(root, task_id)
    record["finished"] = _now()
    path = root / "attempts" / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def describe(record: dict) -> str:
    gate = record.get("gate") or {}
    return (f"{record['task_id']}: {len(record['attempts'])} attempt(s), gate exit "
            f"{gate.get('exit')}, {len(record['promoted'])} document(s) promoted"
            + (f", WROTE OUTSIDE: {', '.join(record['wrote_outside'])}"
               if record["wrote_outside"] else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--search", type=Path)
    parser.add_argument("--prompts", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--no-reviewer", action="store_true")
    parser.add_argument("--timeout", type=float, default=5400.0)
    parser.add_argument("--worker-command", default="",
                        help="a local command that stands in for the model")
    parser.add_argument("--execute", action="store_true",
                        help="make the real calls; without it nothing is spent")
    args = parser.parse_args(argv)
    if args.execute and args.worker_command:
        print("task: --execute and --worker-command name two different runs",
              file=sys.stderr)
        return 2
    try:
        record = run_task(root=args.root, task_id=args.task, sources=args.sources,
                          candidates=args.candidates, search=args.search,
                          prompts=args.prompts, model=args.model, execute=args.execute,
                          worker_command=args.worker_command,
                          reviewer=not args.no_reviewer,
                          max_attempts=args.max_attempts, timeout=args.timeout)
    except (TaskProblem, workdir_mod.WorkdirProblem, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(describe(record))
    if record["wrote_outside"]:
        return 3
    return 0 if (record["gate"] or {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
