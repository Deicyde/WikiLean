#!/usr/bin/env python3
"""Stage 10: stage a task's working directory — everything its prompt names, and no more.

docs/PIPELINE.md section 3 (stage 10). A pipeline worker runs through
``site/agent_sandbox.py``: Read, Grep and Glob inside **one** directory, Write into one
directory under it, and no shell. That boundary is why a worker may read untrusted
article text at all, and it is not moving. What has to move instead is the material: if
a prompt names a file, the file is in the working directory before the call, or the
prompt is asking for something that cannot happen.

So the runner stages, and this module is the one place that says what staging is:

    work/<task-id>/
      task.json                 the task's own manifest, copied from the root
      sources/<source-id>/      the rendered views the prompts send a worker to
      candidates/<source-id>.json   the retrieval's hits, when the run has any
      index/                    the declaration search corpus, with its README
      findings/<task-id>/       **the only directory the worker may write in**
      STAGED.json               what was staged, by name and sha256

Nothing else is there. No gate (it is out of tree, and the runner runs it), no other
task's manifest, no run configuration, no path of the host's. The names are
:data:`WORKER_VIEWS` and :data:`SEARCH_FILES` — copied by name, never globbed, so a
stray file beside a rendered source does not ride along into a model call.

``STAGED.json`` is what makes the free pre-flight check possible: before a paid call,
:func:`check_prompt_names` reads the rendered prompt, pulls the paths out of it and
asks whether each one is there.

    python3 scripts/pipeline/run/workdir.py --root <root> --task <task-id> \\
        --sources <dir> [--candidates <dir>] [--search <dir>] [--check <prompt>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

#: The rendered views a worker's prompts send it to, each named rather than globbed.
#: ``text.txt`` is the reading copy and ``layer.txt`` the text a quote is cut from;
#: the rest place a passage and tell the renderer's two coordinate systems apart.
WORKER_VIEWS = ("text.txt", "layer.txt", "sections.json", "paragraphs.json",
                "formulas.json", "finders.json", "layer_map.json")
#: The search corpus ``index_from_corpus.py --search`` writes. Hard-linked where the
#: filesystem allows it: it is the whole universe, the same bytes for every task.
SEARCH_FILES = ("declarations.tsv", "signatures.tsv", "docstrings.tsv",
                "wikidata.tsv", "README.md")
#: Where the worker writes, relative to its working directory. The second component is
#: the task id, because that is the shape the gate's rule 7 and the corpus both expect.
FINDINGS = "findings"
#: The three directories a prompt names, relative to the working directory.
SOURCES_DIR = "sources"
CANDIDATES_DIR = "candidates"
INDEX_DIR = "index"
#: Where the runner leaves what the gate printed, for the reviewer to read.
GATE_VERDICT = "gate-verdict.txt"
#: The manifest of a staged directory.
STAGED_NAME = "STAGED.json"

#: A path named in a prompt: a backticked relative path, which is how the templates
#: write every one of them. ``<source-id>`` and ``<your task id>`` are placeholders the
#: check fills from the manifest.
_PROMPT_PATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./<>-]*\.(?:json|txt|tsv|md))`")


class WorkdirProblem(ValueError):
    """Material a task cannot be staged from, named rather than staged around."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _place(source: Path, target: Path) -> None:
    """Put ``source`` at ``target``: a hard link where it can be, else a copy.

    A hard link, not a symlink: the sandbox scopes a worker's reads to its working
    directory by the path it resolves to, and a symlink out of the directory resolves
    out of it. A hard link is a second name for the same bytes, inside.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def task_manifest(root: Path, task_id: str) -> dict:
    path = Path(root) / "tasks" / f"{task_id}.json"
    if not path.is_file():
        raise WorkdirProblem(f"no manifest for task {task_id} at {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkdirProblem(f"{path} is not a task manifest")
    return value


def sources_of(manifest: dict) -> list[str]:
    return [str(doc.get("source_id")) for doc in manifest.get("documents") or []
            if doc.get("source_id")]


def stage(*, root: Path, task_id: str, sources: Path, candidates: Path | None = None,
          search: Path | None = None) -> dict:
    """Stage one task's working directory and return ``STAGED.json``.

    Idempotent, and it never touches ``findings/``: a retry re-stages the inputs and
    leaves the attempt before it where the worker can read its own document back.
    """
    root = Path(root)
    manifest = task_manifest(root, task_id)
    work = root / "work" / task_id
    work.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str] = {}

    text = json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    (work / "task.json").write_text(text, encoding="utf-8")
    staged["task.json"] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    named = sources_of(manifest)
    if not named:
        raise WorkdirProblem(f"task {task_id} names no source to stage")
    for sid in named:
        directory = Path(sources) / sid
        if not directory.is_dir():
            raise WorkdirProblem(f"no rendered source for {sid} at {directory}")
        for name in WORKER_VIEWS:
            path = directory / name
            if not path.is_file():
                raise WorkdirProblem(f"{sid}: the prompts name {name} and "
                                     f"{path} is not there; render the source first")
            _place(path, work / SOURCES_DIR / sid / name)
            staged[f"{SOURCES_DIR}/{sid}/{name}"] = sha256_file(path)
        if candidates is not None:
            path = Path(candidates) / f"{sid}.json"
            if not path.is_file():
                raise WorkdirProblem(f"{sid}: the run hands out candidates and "
                                     f"{path} is not there")
            _place(path, work / CANDIDATES_DIR / f"{sid}.json")
            staged[f"{CANDIDATES_DIR}/{sid}.json"] = sha256_file(path)

    if search is not None:
        for name in SEARCH_FILES:
            path = Path(search) / name
            if not path.is_file():
                raise WorkdirProblem(f"the search corpus has no {name} at {path}; "
                                     "build it with index_from_corpus.py --search")
            _place(path, work / INDEX_DIR / name)
            staged[f"{INDEX_DIR}/{name}"] = sha256_file(path)

    (work / FINDINGS / task_id).mkdir(parents=True, exist_ok=True)
    record = {"schema": "wikilean.pipeline.staged/1", "task_id": task_id,
              "sources": named, "findings_dir": f"{FINDINGS}/{task_id}",
              "files": dict(sorted(staged.items()))}
    (work / STAGED_NAME).write_text(
        json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def findings_dir(root: Path, task_id: str) -> Path:
    """Where the worker writes, and the only directory it may write in."""
    return Path(root) / "work" / task_id / FINDINGS / task_id


def documents(root: Path, task_id: str) -> list[Path]:
    """The documents a worker left, sorted."""
    return sorted(p for p in findings_dir(root, task_id).glob("*.json") if p.is_file())


def wrote_outside(root: Path, task_id: str, staged: dict) -> list[str]:
    """Anything in the working directory that is neither staged nor a document.

    The sandbox refuses a write outside the findings directory, and this says so after
    the fact as well: a check that only trusts the boundary cannot notice the day the
    boundary stops holding.
    """
    work = Path(root) / "work" / task_id
    known = set(staged.get("files") or {}) | {STAGED_NAME, GATE_VERDICT}
    out = []
    for path in sorted(work.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(work).as_posix()
        if rel in known or rel.startswith(f"{FINDINGS}/"):
            continue
        out.append(rel)
    return out


# ---- the free pre-flight check ---------------------------------------------------------


def prompt_paths(text: str, *, task_id: str, sources: list[str]) -> list[str]:
    """Every relative path a prompt names, with its placeholders filled.

    The templates write a path in backticks and nowhere else, so this is a read of the
    prompt and not a guess at it: a path the prompt names that staging did not put
    there is the defect the run of 2026-11-06 stopped at.
    """
    out: list[str] = []
    for raw in _PROMPT_PATH.findall(text):
        if raw.startswith("<"):
            continue
        for sid in sources or [""]:
            filled = (raw.replace("<source-id>", sid)
                         .replace("<your task id>", task_id)
                         .replace("<task id>", task_id))
            if "<" in filled or ">" in filled:
                continue
            if filled not in out:
                out.append(filled)
    return out


def check_prompt_names(*, root: Path, task_id: str, prompts: list[Path]) -> dict:
    """Is every path the prompts name in the staged directory? Free, and no model.

    Two paths are written during the task rather than staged before it — the worker's
    own document under ``findings/`` and the gate's output in ``gate-verdict.txt`` —
    and the directory is checked for them instead. Everything else a prompt names is a
    file the agent must be able to open before the call is paid for.
    """
    work = Path(root) / "work" / task_id
    staged_path = work / STAGED_NAME
    if not staged_path.is_file():
        raise WorkdirProblem(f"task {task_id} is not staged: no {staged_path}")
    staged = json.loads(staged_path.read_text(encoding="utf-8"))
    sources = list(staged.get("sources") or [])
    staged_files = set(staged.get("files") or {})
    basenames = {name.rsplit("/", 1)[-1] for name in staged_files}
    present, missing = [], []
    for path in prompts:
        text = Path(path).read_text(encoding="utf-8")
        for rel in prompt_paths(text, task_id=task_id, sources=sources):
            if rel in (GATE_VERDICT,) or rel.startswith(f"{FINDINGS}/") \
                    or rel in present or rel in missing:
                # the worker's document and the gate's own output are written during
                # the task, by the worker and the runner; staging makes their places
                continue
            # a prompt names a path, or — in prose, about a file it has already placed
            # — just its name; both must answer to something staged
            here = (work / rel).is_file() or ("/" not in rel and rel in basenames)
            (present if here else missing).append(rel)
    return {"task_id": task_id, "work": str(work), "sources": sources,
            "present": sorted(present), "missing": sorted(missing),
            "writes_into": str(findings_dir(root, task_id)),
            "writable_dir_exists": findings_dir(root, task_id).is_dir(),
            "ok": not missing and findings_dir(root, task_id).is_dir()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--search", type=Path)
    parser.add_argument("--check", action="append", default=[], type=Path,
                        help="a rendered prompt whose named paths must all be staged")
    args = parser.parse_args(argv)
    try:
        record = stage(root=args.root, task_id=args.task, sources=args.sources,
                       candidates=args.candidates, search=args.search)
        print(f"{args.task}: {len(record['files'])} file(s) staged into "
              f"{args.root}/work/{args.task}")
        if args.check:
            got = check_prompt_names(root=args.root, task_id=args.task,
                                     prompts=args.check)
            print(json.dumps(got, indent=1))
            if not got["ok"]:
                return 1
    except (WorkdirProblem, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
