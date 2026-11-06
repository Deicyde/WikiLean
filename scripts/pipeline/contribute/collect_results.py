#!/usr/bin/env python3
"""The coordinator's side of contribute mode: collect what the contributors sent back.

docs/PIPELINE.md section 3 (stage 11)::

    python3 scripts/pipeline/contribute/collect_results.py --run-id loc-7 \\
        --repo <work-repo clone> --out <dir> [--machine <id>] [--fetch]

Each machine's result lives on its own branch, so this reads it out of the fetched ref
rather than checking it out: the clone's HEAD never moves, and several machines'
results are collected in one go.

**A result is untrusted input**, even though a trusted person pushed it. Every result is
checked against its own ``RESULT.json`` (every file, by sha256, and nothing unnamed),
refused if it carries article text by name, and — when it carries a sealed record set —
put through ``scripts/articles/verify_submission.py``, the intake contract, with the
same refusal codes and HTTP statuses the submission endpoint will answer with. The
contributor ran the same function before pushing, so a refusal here is a surprise and
says so.

What lands under ``--out``::

    <out>/documents/<task-id>/<source-id>.json   every machine's documents, merged
    <out>/host/<machine-id>/...                  each machine's own records
    <out>/set/<machine-id>/...                   each sealed record set, verified
    <out>/COLLECTED.json                         what came from where

Re-gating the documents and running the tally are pipeline stages 7 and 8, which are
**TODO**; the export, stage 6, is built and reads the rows the tally will write.

Exit codes: 0 collected, 2 a result was refused, 3 the collector failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import results as results_mod  # noqa: E402
import workrepo  # noqa: E402

COLLECTED_JSON = "COLLECTED.json"
COLLECTED_SCHEMA = "wikilean.contribute.collected/1"


class CollectProblem(ValueError):
    """A result the coordinator refuses to take."""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def result_dirs(repo: Path, run_id: str, machines: list[str],
                scratch: Path) -> dict[str, Path]:
    """``{machine id: directory}`` of every result for this run.

    A directory already in the working tree (a branch checked out by hand, or a clone
    with no remote) is taken as it stands; anything else is extracted from its fetched
    ref into ``scratch``.
    """
    out: dict[str, Path] = {}
    base = Path(repo) / workrepo.RESULTS / run_id
    if base.is_dir():
        for path in sorted(base.iterdir()):
            if path.is_dir() and (not machines or path.name in machines):
                out[path.name] = path
    prefix = f"{workrepo.RESULT_PREFIX}/{run_id}/"
    for branch in workrepo.branches(repo, f"{prefix}*"):
        machine = branch[len(prefix):]
        if not machine or machine in out or (machines and machine not in machines):
            continue
        target = Path(scratch) / machine
        if target.exists():
            shutil.rmtree(target)
        workrepo.extract(repo, f"refs/remotes/origin/{branch}",
                         f"{workrepo.RESULTS}/{run_id}/{machine}", target)
        landed = target / workrepo.RESULTS / run_id / machine
        if landed.is_dir():
            out[machine] = landed
    return out


def collect(*, repo: Path, run_id: str, out: Path, machines: list[str] | None = None,
            fetch: bool = False, received_at: str | None = None, log=print) -> dict:
    """Fetch, check and lay out every machine's result; returns the collection receipt."""
    clone = workrepo.check_clone(repo)
    if fetch and workrepo.has_remote(clone):
        workrepo.fetch(clone, "+refs/heads/*:refs/remotes/origin/*")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    found = result_dirs(clone, run_id, list(machines or []), out / ".fetched")
    if not found:
        raise CollectProblem(
            f"no result for {run_id} in {clone} "
            f"({workrepo.RESULTS}/{run_id}/<machine-id>/); a contributor pushes "
            f"{workrepo.RESULT_PREFIX}/{run_id}/<machine-id>")
    documents = out / results_mod.DOCUMENTS
    if documents.exists():
        shutil.rmtree(documents)
    receipt = {"schema": COLLECTED_SCHEMA, "run_id": run_id, "collected_at": now(),
               "machines": {}}
    for machine, directory in sorted(found.items()):
        problems = results_mod.check_receipt(directory)
        if problems:
            raise CollectProblem(
                f"the result from {machine} does not match its {results_mod.RESULT_JSON}: "
                + "; ".join(problems[:5])
                + " — fetch the work repo again; a result is never edited in place")
        landed = results_mod.copy_tree(directory / results_mod.DOCUMENTS, documents, "*.json")
        results_mod.copy_tree(directory / results_mod.HOST,
                              out / results_mod.HOST / machine)
        row = {"documents": len(landed)}
        set_dir = directory / results_mod.RECORD_SET
        if (set_dir / "SEAL.json").is_file():
            # the intake contract again: a result is untrusted input, whoever pushed it
            try:
                submission = results_mod.verify_record_set(
                    set_dir, machine_id=machine, received_at=received_at or now())
            except results_mod.ResultProblem as exc:
                raise CollectProblem(
                    f"the sealed set from {machine} is refused by the intake contract, which "
                    f"the contributor ran before pushing, so this is a surprise:\n{exc}") from exc
            results_mod.copy_tree(set_dir, out / results_mod.RECORD_SET / machine)
            row["submission"] = submission
            log(f"  {machine}: set accepted — {submission['records']} record(s), seal "
                f"{submission['seal_sha256'][:12]}")
        receipt["machines"][machine] = row
        log(f"  {machine}: {len(landed)} document(s)")
    (out / COLLECTED_JSON).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n",
                                      encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", required=True, type=Path, help="a clone of the work repo")
    parser.add_argument("--out", required=True, type=Path, help="where the results land")
    parser.add_argument("--machine", dest="machines", action="append", default=[],
                        help="collect only this machine's result (repeatable)")
    parser.add_argument("--fetch", action="store_true",
                        help="fetch the work repo's refs first")
    args = parser.parse_args(argv)
    try:
        receipt = collect(repo=args.repo, run_id=args.run_id, out=args.out,
                          machines=args.machines, fetch=args.fetch)
    except (CollectProblem, results_mod.ResultProblem, workrepo.WorkRepoProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"collecting failed: {exc!r}", file=sys.stderr)
        return 3
    total = sum(row["documents"] for row in receipt["machines"].values())
    print(f"collected {total} document(s) for {args.run_id} from "
          f"{len(receipt['machines'])} machine(s) -> {args.out}")
    print("  next: re-gate them out of tree (scripts/pipeline/gate/verify_locate.py "
          "--findings-dir) and tally the run (scripts/pipeline/tally.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
