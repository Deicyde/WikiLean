#!/usr/bin/env python3
"""Stage 9b: the statement-level cut — the inventory of one revision.

docs/PIPELINE.md section 3 (stage 9b). The inventory is the table of every span of a
source that states something: its kind, its prominence, its scope, and the frozen
``In-text #<hex>`` id the head rule keys it by. One task per source, one model call per
task (plus a reviewer), and the gate freezes the table: every later stage — locate (9c),
the challenge (9d), the tally (8) and the export (6) — reads this cut and never makes
another.

What this module owns:

* **the plan** (:func:`plan_inventory`) — one task per source of a sourceset, each
  task's document carrying the source's pins and the **deterministic finders'**
  passages, pre-filled from ``finders.json`` (stage 9a's renderer). Dropping one of them
  needs a skip naming its span, which gate rule 8 checks: a finder is a floor, not a
  suggestion;
* **the frozen inventory** (:func:`frozen_inventory`, :func:`passage_index`) — a
  harvested inventory run read back as the later stages read it. The locate planner cuts
  tasks from it, and the challenge plan takes each passage's quote and reading-copy span
  from it, because those are the cut's and not the tally's;
* **the prompts**, through :mod:`prompts`, which the root stages beside the tasks.

What it does **not** own: the ``In-text #<hex>`` ids (``gate.locate_text.assign_ids``,
stage 7), the span-end rule (same), the gate's refusals (stage 7) and the root's shape
(``run.plan``, stage 10). This module is the plan and the inputs, and nothing else.

Ported from formal-companion's ``scripts/locate/plan.py`` (its ``locate-inventory``
preset) and ``templates/prompts/locate-inventory*``.

    python3 scripts/pipeline/cut.py --run-id inv-1 --out <root> --manifest <file> \\
        --sources <dir> --universe <file> [--source <id> …]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "run", HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import corpus as corpus_mod  # noqa: E402
import plan as plan_mod  # noqa: E402 — scripts/pipeline/run/plan.py
import sourceset as sourceset_mod  # noqa: E402
import vocab  # noqa: E402

#: an inventory task holds exactly one source: the cut of one article is one call
SOURCES_PER_TASK = 1
#: what a task's document names of the source, beside its finders
DOCUMENT_PINS = ("source_sha", "layer_sha256", "renderer")


class CutProblem(ValueError):
    """An input the cut cannot be planned from, or an inventory that is not one."""


def read_finders(sources_dir: Path, source_id: str) -> list[dict]:
    """A rendered source's ``finders.json``: the deterministic finders' passages."""
    path = Path(sources_dir) / source_id / "finders.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CutProblem(f"{path} is missing: render the source first "
                         f"(scripts/pipeline/fetch_sources.py)") from None
    except ValueError as exc:
        raise CutProblem(f"{path}: not JSON ({exc})") from None
    if not isinstance(value, list):
        raise CutProblem(f"{path}: a finders file is a list")
    return [row for row in value if isinstance(row, dict)]


def task_id(source_id: str) -> str:
    """``inv-<source>``: one task per source, named so a document's path says which."""
    return f"inv-{source_id}"


def task_documents(source_ids, entries: dict, sources_dir: Path, run_id: str) -> list[dict]:
    """One task manifest per source, the finders pre-filled."""
    out = []
    for sid in source_ids:
        entry = entries[sid]
        document = {"source_id": sid, "source_sha": entry.get("html_sha256"),
                    "layer_sha256": entry.get("layer_sha256"),
                    "renderer": entry.get("renderer"),
                    "finders": read_finders(sources_dir, sid)}
        out.append({"task_id": task_id(sid), "run_id": run_id,
                    "mode": vocab.MODE_INVENTORY, "documents": [document]})
    return out


def planned_sources(manifest, *, source_ids=()) -> tuple[list[str], list[dict]]:
    """``(the sources a cut plans, the ones it skips with a reason)``.

    A page with a flag true is no concept article and a source with no layer is not
    rendered: both are skipped by name rather than planned and failed
    (:func:`sourceset.skip_reason`).
    """
    wanted = list(source_ids) or list(manifest.preferred().values())
    planned, skipped = [], []
    for sid in wanted:
        entry = manifest.get(sid)
        if entry is None:
            raise CutProblem(f"the sourceset holds no entry for {sid}")
        reason = sourceset_mod.skip_reason(entry)
        if reason:
            skipped.append({"source_id": sid, "reason": reason})
        else:
            planned.append(sid)
    return sorted(planned), sorted(skipped, key=lambda row: row["source_id"])


def plan_inventory(*, run_id: str, out: Path, sourceset_dir: Path, universe: Path,
                   source_ids=(), sourceset_id: str | None = None, **plan_args) -> dict:
    """Write an inventory root: one task per source, the finders pre-filled."""
    ss = sourceset_mod.Sourceset.open(sourceset_dir, sourceset_id)
    manifest = ss.latest()
    planned, skipped = planned_sources(manifest, source_ids=source_ids)
    if not planned:
        raise CutProblem(f"{ss.id}: no source to cut (skipped: {len(skipped)})")
    sources_dir = ss.dir / "sources"
    documents = task_documents(planned, manifest.entries, sources_dir, run_id)
    built = plan_mod.plan(run_id=run_id, mode=vocab.MODE_INVENTORY, out=Path(out),
                          universe=Path(universe),
                          manifest=ss.manifest_path(manifest.version),
                          sources=sources_dir, source_ids=planned,
                          entries=manifest.entries, task_documents=documents,
                          **plan_args)
    built["skipped"] = skipped
    return built


# ---- the frozen inventory, as the later stages read it ----------------------------


def frozen_inventory(corpus_dir: Path, sources_dir: Path | None = None
                     ) -> dict[str, list[dict]]:
    """``{source_id: [passage]}`` of a harvested inventory run, in document order.

    With ``sources_dir``, each passage also gets its **reading-copy span**
    (``text_span``), mapped from its layer span through the source's
    ``layer_map.json``: the layer is what a quote is cut from and the reading copy is
    what a worker reads, and only the renderer's map relates the two. A passage that
    already carries one keeps it.
    """
    inventory = plan_mod.frozen_inventory(Path(corpus_dir))
    if sources_dir is None:
        return inventory
    import render_web  # noqa: PLC0415 — only a plan that reads sources needs it

    for sid, passages in inventory.items():
        directory = Path(sources_dir) / sid
        if not (directory / "layer_map.json").is_file():
            raise CutProblem(f"{directory}/layer_map.json is missing: a locate plan maps "
                             f"each passage's span onto the reading copy with it")
        segments = render_web.LayerMap.read(directory).segments
        for passage in passages:
            if passage.get("text_span") is None:
                passage["text_span"] = render_web.text_span(segments,
                                                            passage.get("span"))
    return inventory


def passage_index(corpus_dir: Path) -> dict[tuple[str, str], dict]:
    """``{(source_id, item): passage}`` of a harvested inventory run.

    This is where a passage's quote and reading-copy span live: the tally's outcome rows
    carry neither, because both are the cut's and a re-cut moves them.
    """
    out: dict[tuple[str, str], dict] = {}
    for sid, passages in frozen_inventory(Path(corpus_dir)).items():
        for passage in passages:
            item = str(passage.get("item") or "")
            if not item:
                continue
            if (sid, item) in out:
                raise CutProblem(f"the inventory gives {sid} two passages {item}")
            out[(sid, item)] = passage
    return out


def counts(inventory: dict[str, list[dict]]) -> dict:
    """What a cut produced: sources, passages, and the passages by scope and prominence."""
    passages = [p for rows in inventory.values() for p in rows]
    by = lambda key: {  # noqa: E731
        value: sum(1 for p in passages if p.get(key) == value)
        for value in sorted({str(p.get(key)) for p in passages})}
    return {"sources": len(inventory), "passages": len(passages),
            "by_scope": by("scope"), "by_prominence": by("prominence"),
            "by_kind": by("passage_kind"),
            "located": sum(1 for p in passages
                           if p.get("scope") in vocab.LOCATED_SCOPES)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path, help="the root to write")
    parser.add_argument("--sourceset", required=True, type=Path,
                        help="the sourceset directory")
    parser.add_argument("--sourceset-id")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--index", type=Path, help="the declaration index")
    parser.add_argument("--prompts", action="store_true",
                        help="render the prompts into the root")
    parser.add_argument("--source", action="append", default=[], dest="source_ids")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-cost", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        built = plan_inventory(run_id=args.run_id, out=args.out,
                               sourceset_dir=args.sourceset,
                               sourceset_id=args.sourceset_id, universe=args.universe,
                               source_ids=args.source_ids, index=args.index,
                               render_prompts=args.prompts, model=args.model,
                               max_cost=args.max_cost)
    except (CutProblem, sourceset_mod.SourcesetError, plan_mod.PlanProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{args.run_id} [{vocab.MODE_INVENTORY}]: {len(built['tasks'])} task(s), "
          f"{len(built['skipped'])} source(s) skipped -> {args.out}")
    for row in built["skipped"][:10]:
        print(f"  skipped {row['source_id']}: {row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
