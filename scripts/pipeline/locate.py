#!/usr/bin/env python3
"""Stage 9c: the locate run — retrieval, the tasks and the prompts.

docs/PIPELINE.md section 3 (stage 9c). A locate run takes a **frozen inventory** (stage
9b) and asks, for each ``formal_math`` passage, where in the universe's libraries it is
stated: a location with a relation, a checked absence with its receipt, or an
unresolved outcome.

This module is the three pieces stage 10 does not own:

* **the retrieval** — :mod:`candidates`, over WikiLean's declaration index. A source's
  curated hits bind gate rule 4 (the worker accepts or rejects each one with a reason);
  its lexical hits are a hint;
* **the task cut and the manifests** — at most :data:`vocab.MAX_PASSAGES_PER_LOCATE_TASK`
  passages of one source to a task, in layer order, so a re-plan of an unchanged
  inventory cuts the same tasks. ``run.plan`` does the cutting and writes the root;
* **the prompts** — :mod:`prompts`, rendered from the run's own paths.

The root, the staged gate, ``gate.json``'s pins and ``run.json`` are stage 10's
(``scripts/pipeline/run/plan.py``), and nothing here duplicates them.

    python3 scripts/pipeline/locate.py --run-id loc-1 --out <root> \\
        --inventory <corpus dir> --sourceset <dir> --universe <file> \\
        [--index <file>] [--candidates <dir>] [--per-task 8]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "run", HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import candidates as candidates_mod  # noqa: E402
import cut as cut_mod  # noqa: E402
import plan as plan_mod  # noqa: E402 — scripts/pipeline/run/plan.py
import sourceset as sourceset_mod  # noqa: E402
import vocab  # noqa: E402


class LocateProblem(ValueError):
    """An input a locate run cannot be planned from."""


def plan_locate(*, run_id: str, out: Path, inventory: Path, sourceset_dir: Path,
                universe: Path, index: Path | None = None,
                candidates: Path | None = None, retrieve: bool = False,
                source_ids=(), sourceset_id: str | None = None,
                per_task: int = vocab.MAX_PASSAGES_PER_LOCATE_TASK, **plan_args) -> dict:
    """Retrieve (when asked), then write the locate root over a frozen inventory."""
    ss = sourceset_mod.Sourceset.open(sourceset_dir, sourceset_id)
    manifest = ss.latest()
    frozen = cut_mod.frozen_inventory(Path(inventory), ss.dir / "sources")
    wanted = sorted(set(source_ids) or set(frozen))
    missing = [sid for sid in wanted if sid not in frozen]
    if missing:
        raise LocateProblem(f"the inventory holds no document for "
                            f"{', '.join(missing[:5])}")
    if retrieve:
        if index is None or candidates is None:
            raise LocateProblem("retrieval needs both --index and --candidates")
        candidates_mod.run(sourceset_dir=ss.dir, index=Path(index),
                           universe=Path(universe), out=Path(candidates),
                           source_ids=wanted, sourceset_id=ss.id)
    return plan_mod.plan(run_id=run_id, mode=vocab.MODE_LOCATE, out=Path(out),
                         inventory=Path(inventory), inventory_passages=frozen,
                         universe=Path(universe),
                         manifest=ss.manifest_path(manifest.version),
                         sources=ss.dir / "sources", entries=manifest.entries,
                         candidates=candidates,
                         index=index, per_task=per_task, source_ids=wanted, **plan_args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path, help="the root to write")
    parser.add_argument("--inventory", required=True, type=Path,
                        help="the harvested inventory run's corpus directory")
    parser.add_argument("--inventory-run", default="", help="its run id")
    parser.add_argument("--sourceset", required=True, type=Path)
    parser.add_argument("--sourceset-id")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--index", type=Path, help="the declaration index")
    parser.add_argument("--candidates", type=Path, help="the candidates directory")
    parser.add_argument("--retrieve", action="store_true",
                        help="run the retrieval into --candidates first")
    parser.add_argument("--prompts", action="store_true",
                        help="render this pipeline's own prompts into the root")
    parser.add_argument("--per-task", type=int,
                        default=vocab.MAX_PASSAGES_PER_LOCATE_TASK)
    parser.add_argument("--source", action="append", default=[], dest="source_ids")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-cost", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        built = plan_locate(run_id=args.run_id, out=args.out, inventory=args.inventory,
                            sourceset_dir=args.sourceset, sourceset_id=args.sourceset_id,
                            universe=args.universe, index=args.index,
                            candidates=args.candidates, retrieve=args.retrieve,
                            source_ids=args.source_ids, per_task=args.per_task,
                            inventory_run=args.inventory_run,
                            render_prompts=args.prompts, model=args.model,
                            max_cost=args.max_cost)
    except (LocateProblem, cut_mod.CutProblem, candidates_mod.CandidateProblem,
            sourceset_mod.SourcesetError, plan_mod.PlanProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    passages = sum(len(doc["passages"]) for task in built["tasks"]
                   for doc in task["documents"])
    print(f"{args.run_id} [{vocab.MODE_LOCATE}]: {len(built['tasks'])} task(s), "
          f"{passages} passage(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
