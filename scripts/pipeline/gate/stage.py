#!/usr/bin/env python3
"""Stage the gate out of tree: a directory a worker cannot edit, run by absolute path.

docs/PIPELINE.md section 3 (stage 7). The gate's whole value rests on it not being in
the worktree the worker writes into: a worker that can edit the gate has no gate. This
writes a self-contained gate directory —

    <gate dir>/verify_locate.py     the rules
    <gate dir>/passage_ids.py       the runner's pre-gate step: the ids and the spans
    <gate dir>/locate_text.py       the head rule, the quote and the blocks
    <gate dir>/anchor_resolve.py    WikiLean's own text-layer whitespace rule
    <gate dir>/records.py           the record builder rule 1 holds a location to
    <gate dir>/keys.py  vocab.py  links.py  index.py  items.py
    <gate dir>/gate.json            the run's pins
    <gate dir>/tasks/<task-id>.json one per task, the frozen passages it was handed

— and nothing else: no workspace, no corpus, no article text. Every file is copied by
name, and :func:`stage` returns the sha256 of each, so ``run.yaml`` can record what was
staged and a re-stage of an unchanged checkout is byte for byte the same directory.

    python3 scripts/pipeline/gate/stage.py --out <dir> --gate-config <file> \\
        [--task <file> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
ROOT = PIPELINE.parents[1]

#: What a gate directory holds, each named rather than globbed: a glob would carry a
#: stray file onto a run host, and the list is the contract with the run.
GATE_FILES = (
    (HERE / "verify_locate.py", "verify_locate.py"),
    (HERE / "passage_ids.py", "passage_ids.py"),
    (HERE / "locate_text.py", "locate_text.py"),
    (ROOT / "site" / "anchor_resolve.py", "anchor_resolve.py"),
    (PIPELINE / "records.py", "records.py"),
    (PIPELINE / "keys.py", "keys.py"),
    (PIPELINE / "vocab.py", "vocab.py"),
    (PIPELINE / "items.py", "items.py"),
    (PIPELINE / "index.py", "index.py"),
    (PIPELINE / "links.py", "links.py"),
)


class StageProblem(ValueError):
    """A gate directory that cannot be written where it was asked for."""


def check_target(out: Path) -> Path:
    """Where a gate directory may go: never inside this checkout, never a symlink."""
    target = Path(out).expanduser()
    if target.is_symlink():
        raise StageProblem(f"{target} is a symlink; a gate directory is a real directory")
    resolved = target.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise StageProblem(f"refusing to stage the gate inside this checkout ({ROOT}): the "
                           "gate runs out of tree, and a gate a worker can edit is no gate")
    return target


def stage(out: Path, gate_config: Path, tasks: list[Path] | None = None) -> dict:
    """Write the gate directory; return ``{relative path: sha256}`` of every file in it."""
    target = check_target(out)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for source, name in GATE_FILES:
        if not source.is_file():
            raise StageProblem(f"the gate needs {source}, which is not in this checkout")
        shutil.copyfile(source, target / name)
        written[name] = hashlib.sha256((target / name).read_bytes()).hexdigest()
    config = Path(gate_config)
    if not config.is_file():
        raise StageProblem(f"no gate.json at {config}")
    shutil.copyfile(config, target / "gate.json")
    written["gate.json"] = hashlib.sha256((target / "gate.json").read_bytes()).hexdigest()
    if tasks:
        (target / "tasks").mkdir(exist_ok=True)
        for task in tasks:
            name = f"tasks/{Path(task).name}"
            shutil.copyfile(task, target / name)
            written[name] = hashlib.sha256((target / name).read_bytes()).hexdigest()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="the gate directory to write")
    parser.add_argument("--gate-config", required=True, type=Path, help="the run's gate.json")
    parser.add_argument("--task", action="append", default=[], type=Path,
                        help="a task manifest to stage (repeatable)")
    args = parser.parse_args(argv)
    try:
        written = stage(args.out, args.gate_config, args.task)
    except StageProblem as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(written, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
