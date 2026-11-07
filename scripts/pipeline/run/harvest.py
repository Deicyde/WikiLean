#!/usr/bin/env python3
"""Stage 10: bring a run's documents home, with a receipt that proves the corpus is whole.

docs/PIPELINE.md section 3 (stage 10). **The harvest is not a copy, it is a
reconciliation.** Every number downstream — the tally's counts, the safety metric, the
store — is a function of the corpus, so a corpus that is quietly a file short makes every
count quietly wrong, and in the one recorded case all three counting errors ran toward
"cleaner". So:

1. list the **source** — that list is the expected set, by NAME, never a number someone
   remembered;
2. transfer;
3. list the **destination** and diff the two sets;
4. write ``RECEIPT.json``: the expected and actual counts, the missing and extra names, a
   sha256 manifest of what landed, the gate's verdict and the run's cost;
5. **refuse to mark the harvest complete on any mismatch.**

Re-running is safe and is the normal way to close a gap: the receipt records the previous
count and the delta, so a second harvest says what it gained and what it lost.

The receipt is complete only when nothing is missing, every file matches the source byte
for byte where checksums were collected, every file that landed reads as a document, and
the gate passed where one was run. The cost block never affects ``complete``: a run's
documents are whole or not whichever way anyone priced them.

Ported from ``harvest/harvest.py``. What changes: the transfer is a plain directory copy
or a command the operator names, rather than an ssh/rsync leg built into the module — a
contributor's work comes home over the git transport of stage 11, and a run host's over
whatever the host has; what moves here is the **rule**, which is the part that was wrong
once.

    python3 scripts/pipeline/run/harvest.py --run-id <id> --from <dir> --to <dir> \\
        [--usage <dir>] [--gate-log <file>] [--run-config <file>] [--no-copy]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import corpus as corpus_mod  # noqa: E402
import cost as cost_mod  # noqa: E402

RECEIPT_NAME = corpus_mod.RECEIPT_NAME
#: How many missing names a message spells out before it truncates.
MAX_NAMED = 20


class HarvestProblem(ValueError):
    """A harvest that cannot be reconciled at all, as against one that is incomplete."""


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(directory: Path) -> dict[str, str]:
    """``{relative path: sha256}`` over every document of a corpus directory."""
    root = Path(directory)
    return {corpus_mod.document_name(path, root): sha256_file(path)
            for path in corpus_mod.document_files(root)}


def source_names(directory: Path) -> list[str]:
    """The expected set: every document name the SOURCE holds, sorted."""
    root = Path(directory)
    return sorted(corpus_mod.document_name(path, root)
                  for path in corpus_mod.document_files(root))


def transfer(source: Path, dest: Path) -> int:
    """Copy every document of the source into the destination; return how many."""
    source, dest = Path(source), Path(dest)
    if source.resolve() == dest.resolve():
        return 0
    copied = 0
    for path in corpus_mod.document_files(source):
        rel = corpus_mod.document_name(path, source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied += 1
    return copied


def malformed(directory: Path) -> list[str]:
    """Every file that landed and cannot be read as a document object."""
    root = Path(directory)
    out = []
    for path in corpus_mod.document_files(root):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(corpus_mod.document_name(path, root))
            continue
        if not isinstance(value, dict):
            out.append(corpus_mod.document_name(path, root))
    return out


def document_counts(directory: Path) -> dict:
    """What landed, by source and by mode: the receipt's ``by_source`` and ``by_mode``.

    A file :func:`malformed` already named is passed over here: the receipt names it
    once, in the list that makes the harvest incomplete, and counts what it can of the
    rest rather than refusing to say anything.
    """
    root = Path(directory)
    by_source: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    passages = 0
    for path in corpus_mod.document_files(root):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        sid = str(doc.get("source_id") or "")
        if sid:
            by_source[sid] = by_source.get(sid, 0) + 1
        mode = str(doc.get("mode") or "")
        if mode:
            by_mode[mode] = by_mode.get(mode, 0) + 1
        passages += len(doc.get("passages") or [])
    return {"by_source": dict(sorted(by_source.items())),
            "by_mode": dict(sorted(by_mode.items())), "passages": passages}


def gate_summary(log_path: Path | None) -> dict | None:
    """The gate's verdict over the run, from the gate log the host kept.

    ``{ok, calls, tasks, failed, retried, rejected}`` over the calls that judged a
    change set. A run whose gate rejected a contribution that then merged anyway is not
    a gated run, and the receipt says so; a log that cannot be read is ``None`` — no
    gate ran that this harvest can see, which is not the same as one that passed.

    **A task's verdict is its last call, not its first.** The runner retries a refused
    document with the gate's own refusals and gates it again (:mod:`task`), so a log of
    one shard holds an attempt's refusal beside the pass that replaced it, and only the
    document whose last call passed is promoted into the corpus at all. Counting the
    superseded refusal would make every run with a retry "not gated" while the corpus
    holds nothing the gate refused — the opposite of what this guard is for. What is
    counted is ``failed``: tasks whose **last** call refused. ``retried`` says how many
    were refused earlier and passed later, so a retry is still visible and never
    silent.
    """
    if log_path is None:
        return None
    path = Path(log_path)
    if not path.is_file():
        return None
    calls = 0
    last: dict[str, bool] = {}
    ever_failed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("changed_files") is not True:
            continue
        if record.get("pre_merge") is True:
            continue
        calls += 1
        # a call with no task id is its own verdict and cannot be superseded by another
        task = str(record.get("task_id") or f"call-{calls}")
        last[task] = bool(record.get("ok"))
        if not record.get("ok"):
            ever_failed.add(task)
    rejected = sorted(task for task, ok in last.items() if not ok)
    return {"ok": True, "calls": calls, "tasks": len(last), "failed": len(rejected),
            "retried": len(ever_failed - set(rejected)), "rejected": rejected}


def build_receipt(*, run_id: str, expected: Iterable[str], dest: Path,
                  source: Mapping | None = None,
                  source_manifest: Mapping[str, str] | None = None,
                  previous: Mapping | None = None, gate: Mapping | None = None,
                  cost: Mapping | None = None, run_config: Mapping | None = None) -> dict:
    """Reconcile the destination against the expected set and return the receipt."""
    dest = Path(dest)
    actual = source_names(dest)
    expected_set, actual_set = set(expected), set(actual)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    got = manifest(dest)
    mismatched = sorted(name for name, sha in (source_manifest or {}).items()
                        if name in got and got[name] != sha)
    # checksums asked for and a landed file with none from the source: it was never
    # compared, so the receipt may not say it was — fail closed
    unverified = sorted(set(got) - set(source_manifest or {})) \
        if source_manifest is not None else []
    bad = malformed(dest)
    gate_ok = True if gate is None else bool(gate.get("ok")) and not gate.get("failed")
    receipt = {
        "schema": "wikilean.pipeline.receipt/1",
        "run_id": run_id,
        "harvested_at": iso_now(),
        "source": dict(source or {}),
        "destination": str(dest),
        "expected": {"count": len(expected_set), "names": sorted(expected_set)},
        "actual": {"count": len(actual_set)},
        "missing": missing,
        "extra": extra,
        "sha_mismatched": mismatched,
        "sha_verified": bool(source_manifest) and not unverified,
        "sha_unverified": unverified,
        "malformed": bad,
        "complete": (not missing and not mismatched and not unverified and not bad
                     and gate_ok),
        "manifest_algo": "sha256",
        "manifest": got,
        "gate": dict(gate) if gate is not None else None,
        "cost": dict(cost) if cost is not None else None,
        **document_counts(dest),
    }
    if run_config is not None:
        receipt["run"] = {"tasks": (run_config.get("tasks") or {}),
                          "mode": run_config.get("mode")}
    if previous:
        before = set((previous.get("manifest") or {}).keys())
        receipt["previous"] = {
            "harvested_at": previous.get("harvested_at"),
            "actual": (previous.get("actual") or {}).get("count"),
            "complete": previous.get("complete"),
            "gained": sorted(actual_set - before),
            "lost": sorted(before - actual_set),
        }
    return receipt


def describe(receipt: Mapping) -> str:
    """One line a person reads, and the lines that say what is wrong."""
    lines = [f"{receipt['run_id']}: {receipt['actual']['count']} of "
             f"{receipt['expected']['count']} document(s), "
             f"{'complete' if receipt['complete'] else 'INCOMPLETE'}"]
    for key, what in (("missing", "missing"), ("extra", "not expected"),
                      ("sha_mismatched", "not the source's bytes"),
                      ("sha_unverified", "never compared with the source"),
                      ("malformed", "unreadable as a document")):
        names = list(receipt.get(key) or [])
        if names:
            shown = ", ".join(names[:MAX_NAMED])
            more = f" and {len(names) - MAX_NAMED} more" if len(names) > MAX_NAMED else ""
            lines.append(f"  {len(names)} {what}: {shown}{more}")
    gate = receipt.get("gate")
    if isinstance(gate, Mapping):
        lines.append(f"  gate: {gate.get('calls')} validation call(s), "
                     f"{gate.get('failed')} failed")
    if receipt.get("cost"):
        lines.append(f"  cost: ${float(receipt['cost'].get('usd') or 0):.2f} over "
                     f"{receipt['cost'].get('envelope_count')} envelope(s)")
    return "\n".join(lines)


def harvest(*, run_id: str, source: Path, dest: Path, copy: bool = True,
            usage: Path | None = None, gate_log: Path | None = None,
            run_config: Mapping | None = None) -> dict:
    """List, transfer, list again, diff, and write the receipt. Returns it."""
    source, dest = Path(source), Path(dest)
    if not source.is_dir():
        raise HarvestProblem(f"no source directory at {source}")
    expected = source_names(source)
    expected_manifest = manifest(source)
    dest.mkdir(parents=True, exist_ok=True)
    previous = corpus_mod.receipt(dest)
    if copy:
        transfer(source, dest)
    receipt = build_receipt(
        run_id=run_id, expected=expected, dest=dest,
        source={"kind": "directory", "path": str(source)},
        source_manifest=expected_manifest, previous=previous,
        gate=gate_summary(gate_log),
        cost=cost_mod.cost_block(usage) if usage is not None else None,
        run_config=run_config)
    (dest / RECEIPT_NAME).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--from", dest="source", required=True, type=Path,
                        help="where the documents are now")
    parser.add_argument("--to", dest="dest", required=True, type=Path,
                        help="the corpus directory they land in")
    parser.add_argument("--no-copy", action="store_true",
                        help="reconcile only: the documents are already in place")
    parser.add_argument("--usage", type=Path, help="the agents' usage envelopes")
    parser.add_argument("--gate-log", type=Path, help="the gate log the host kept")
    parser.add_argument("--run-config", type=Path, help="the run's run.json, for its task count")
    args = parser.parse_args(argv)
    config = None
    if args.run_config and args.run_config.is_file():
        config = json.loads(args.run_config.read_text(encoding="utf-8"))
    try:
        receipt = harvest(run_id=args.run_id, source=args.source, dest=args.dest,
                          copy=not args.no_copy, usage=args.usage, gate_log=args.gate_log,
                          run_config=config)
    except (HarvestProblem, corpus_mod.CorpusProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(describe(receipt))
    return 0 if receipt["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
