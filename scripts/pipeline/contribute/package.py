#!/usr/bin/env python3
"""A contribute package: one planned locate run, free of every machine's paths.

docs/PIPELINE.md section 3 (stage 11). A package is what the coordinator's planner wrote
for a contributor host, copied into the work repo::

    packages/<run-id>/package.json   this manifest
    packages/<run-id>/<the plan>     the run's own files, exactly as planned
    packages/<run-id>/sources.jsonl  the sources' pins: one row per source, no bytes
    packages/<run-id>/universe.json  the universe (metadata about public libraries)

**Never in a package**: a source's text layer, its raw or canonical HTML bytes, or a
candidates file. The contributor refetches each page at its pinned revision and
regenerates the candidates, and every sha is checked against ``package.json``. That is
not only a size rule — WikiLean's work repo is a private repository that strangers' Macs
clone, and an article's bytes are the one thing that must not be in it.

**No path of the coordinator's machine travels.** A plan for a contributor host roots
every path it writes at :data:`ROOT_PLACEHOLDER`; :func:`machine_paths` is the check
that nothing slipped through, and a package that still names a directory is a refusal,
never a rewrite. A shebang and a cache path under ``~/.cache`` are the same on every
machine, so the check is the three roots a package must not name, not "an absolute
path": a check that refuses a shebang teaches nobody anything.

**What a plan is, is stage 9c's, and stage 9c is TODO.** This module is the container:
it takes a planned-run directory as it stands, checks it, and writes the manifest. When
the planner lands, it writes that directory and nothing here changes.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _path in (str(HERE), str(PIPELINE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import keys  # noqa: E402
import vocab  # noqa: E402
import workrepo  # noqa: E402

PACKAGE_SCHEMA = "wikilean.contribute.package/1"
PACKAGE_JSON = "package.json"
SOURCES_JSONL = "sources.jsonl"
UNIVERSE_JSON = "universe.json"
#: What stands for the contributor's own root in every path a package names. The planner
#: writes it; a contributor substitutes its own working directory.
ROOT_PLACEHOLDER = "{{WIKILEAN_CONTRIB_ROOT}}"
#: What stands for the package directory itself in a recorded command: a curated input is
#: a file, and the publisher's path to it is no path of the contributor's, so the file
#: travels in the package and the argument names it there.
PACKAGE_PLACEHOLDER = "{{WIKILEAN_PACKAGE}}"
#: Where a file a recorded command names travels.
INPUTS = "inputs"
#: What a package may never carry, whatever directory it sits in: the article.
REFUSED_NAMES = ("layer.txt", "body.html", "source.raw.html", "text.txt", "head.txt",
                 "reading.txt", "layer_map.json", "sections.json", "blocks.json",
                 "candidates.jsonl")


class PackageProblem(ValueError):
    """A package this transport refuses to write or to read."""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def package_dir(repo: Path, package: str) -> Path:
    """``<clone>/packages/<package>`` — the id is a run id, so it names no path."""
    return Path(repo) / workrepo.PACKAGES / check_run_id(package)


def check_run_id(package: Any) -> str:
    if not isinstance(package, str) or not vocab.RUN_ID_RE.match(package):
        raise PackageProblem(f"{package!r} is not a run id: letters, digits, . _ - , at most 64")
    return package


def available(repo: Path) -> list[str]:
    """Every package in the clone, by run id."""
    root = Path(repo) / workrepo.PACKAGES
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if (path / PACKAGE_JSON).is_file())


def machine_roots(extra: list[str] | None = None) -> list[str]:
    """The directories of this machine a package must never name.

    This checkout (a generated comment could carry its path) and the home directory above
    it, plus anything the caller adds (the coordinator's own run directory). ``/`` is
    never one of them: everything names that.
    """
    roots = [str(Path.home()), str(PIPELINE.parents[1])]
    roots.extend(str(Path(path).expanduser().resolve()) for path in extra or [])
    return sorted({root for root in roots if root not in ("", "/")}, key=len, reverse=True)


def machine_paths(text: str, roots: list[str]) -> list[str]:
    """Which of ``roots`` ``text`` names; empty is the goal."""
    return [root for root in roots if root in text]


def refused_names(paths: list[Path]) -> list[str]:
    """The names among ``paths`` that are article text, whatever directory they sit in."""
    return sorted({p.name for p in paths if p.name in REFUSED_NAMES})


#: The planned root's staged working directories. They hold the coordinator's own
#: copy of the article text (``run/workdir.py``), so they never travel: a package
#: carries the plan, and the contributor stages its own after refetching the pinned
#: revisions. Everything else of the root does travel.
NOT_PACKAGED = ("work",)


def _files(root: Path) -> list[Path]:
    root = Path(root)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts
                  and p.relative_to(root).parts[0] not in NOT_PACKAGED)


def build(plan_dir: Path, *, run_id: str, repo: Path, sources: list[dict],
          universe: dict, mode: str = "locate", tasks: list[str] | None = None,
          max_cost: float | None = None, producer_commit: str | None = None,
          corpus_sha256: str | None = None, inputs: list[Path] | None = None,
          recorded_argv: list[str] | None = None,
          extra_roots: list[str] | None = None) -> dict:
    """Copy a planned run into ``packages/<run-id>/`` and write its manifest.

    ``plan_dir`` is the planner's output directory, rooted at :data:`ROOT_PLACEHOLDER`,
    less its staged working directories (:data:`NOT_PACKAGED`): those hold the
    coordinator's copy of the article text, and the contributor stages its own from
    the revisions it refetches.
    ``sources`` are the sourceset entries, pins only; ``universe`` is the universe
    document. ``inputs`` are files a recorded command names, which travel under
    ``inputs/`` so the recorded argument can name them there.
    """
    plan_dir = Path(plan_dir)
    if not plan_dir.is_dir():
        raise PackageProblem(f"{plan_dir} is not a planned-run directory")
    target = package_dir(repo, run_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    staged = _files(plan_dir)
    named = refused_names(staged)
    if named:
        raise PackageProblem(
            f"the plan would carry article text ({', '.join(named)}); a package carries the "
            "plan, the sources' pins and the universe, and the contributor refetches the "
            "pages at their pinned revisions")
    for path in staged:
        copy = target / path.relative_to(plan_dir)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    for path in inputs or []:
        copy = target / INPUTS / Path(path).name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)

    (target / SOURCES_JSONL).write_text(
        "".join(keys.canonical_line({key: entry.get(key) for key in vocab.SOURCE_KEYS})
                for entry in sources), encoding="utf-8")
    (target / UNIVERSE_JSON).write_text(keys.canonical_line(universe), encoding="utf-8")

    roots = machine_roots(extra_roots)
    for path in _files(target):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found = machine_paths(text, roots)
        if found:
            raise PackageProblem(
                f"{path.relative_to(target)} names this machine's {found[0]}; a package is "
                f"rooted at {ROOT_PLACEHOLDER} and names no directory of the coordinator's")

    manifest = {
        "schema": PACKAGE_SCHEMA,
        "package": run_id,
        "run_id": run_id,
        "mode": mode,
        "created": now(),
        "producer_commit": producer_commit,
        "max_cost": max_cost,
        "tasks": sorted(tasks or []),
        "sources": [{"source_id": entry["source_id"], "pageid": entry["pageid"],
                     "revision": entry["revision"], "html_sha256": entry.get("html_sha256"),
                     "layer_sha256": entry.get("layer_sha256"),
                     "renderer": entry.get("renderer")}
                    for entry in sorted(sources, key=lambda e: str(e.get("source_id")))],
        "universe": {"id": universe.get("id"),
                     "universe_sha256": universe.get("universe_sha256"),
                     "corpus_sha256": corpus_sha256},
        "recorded_argv": list(recorded_argv or []),
        "files": {},
    }
    manifest["files"] = {p.relative_to(target).as_posix(): keys.sha256_file(p)
                         for p in _files(target)}
    (target / PACKAGE_JSON).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8")
    return manifest


def read(directory: Path) -> dict:
    """A package's manifest, with every file it names checked against its sha256."""
    directory = Path(directory)
    path = directory / PACKAGE_JSON
    if not path.is_file():
        raise PackageProblem(f"{directory} holds no {PACKAGE_JSON}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise PackageProblem(f"{path}: schema is {manifest.get('schema')!r}, not "
                             f"{PACKAGE_SCHEMA!r}")
    problems = check(directory, manifest)
    if problems:
        raise PackageProblem(f"{directory} does not match its {PACKAGE_JSON}: "
                             + "; ".join(problems[:5]))
    return manifest


def check(directory: Path, manifest: dict) -> list[str]:
    """Every problem a contributor should see before it runs anything."""
    directory = Path(directory)
    problems: list[str] = []
    named = dict(manifest.get("files") or {})
    for rel, sha in sorted(named.items()):
        target = directory / rel
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            problems.append(f"{rel}: the manifest names a path outside the package")
        elif target.is_symlink():
            problems.append(f"{rel}: a symlink")
        elif not target.is_file():
            problems.append(f"{rel}: missing")
        elif keys.sha256_file(target) != sha:
            problems.append(f"{rel}: sha256 does not match {PACKAGE_JSON}")
    for target in _files(directory):
        rel = target.relative_to(directory).as_posix()
        if rel != PACKAGE_JSON and rel not in named:
            problems.append(f"{rel}: not in {PACKAGE_JSON}")
    named_text = refused_names([directory / rel for rel in named])
    if named_text:
        problems.append(f"the package carries article text ({', '.join(named_text)})")
    return problems


def check_sources(manifest: dict, rendered: dict[str, dict]) -> list[str]:
    """Whether the sources a contributor rendered are the ones the package pinned.

    ``rendered`` is ``{source_id: {html_sha256, layer_sha256, renderer}}`` as this
    machine rendered them. A refetch of an old revision expands templates as they are
    *now*, so a page can render to another layer than the coordinator's; such a source is
    named here and nothing is run against it (docs/PIPELINE.md section 9, item 6).
    """
    problems: list[str] = []
    for pin in manifest.get("sources") or []:
        sid = str(pin.get("source_id"))
        got = rendered.get(sid)
        if got is None:
            problems.append(f"{sid}: not rendered on this machine")
            continue
        for field in ("html_sha256", "layer_sha256", "renderer"):
            if pin.get(field) is not None and got.get(field) != pin.get(field):
                problems.append(f"{sid}: {field} is {str(got.get(field))[:12]} here and the "
                                f"package pins {str(pin.get(field))[:12]}")
    return problems
