#!/usr/bin/env python3
"""A contribute package: one planned locate run, free of every machine's paths.

docs/PIPELINE.md section 3 (stage 11). A package is what the coordinator's planner wrote
for a contributor host, copied into the work repo::

    packages/<run-id>/package.json   this manifest
    packages/<run-id>/<the plan>     the run's own files, exactly as planned
    packages/<run-id>/sources.jsonl  the sources' pins: one row per source, no bytes
    packages/<run-id>/universe.json  the universe (metadata about public libraries)

**What of the article travels, and where** (Jack's ruling of 2026-11-13). A package
may carry each pinned source's fetched bytes, the raw ``action=parse`` response, at
``sources/<sid>/source.raw.html``; its candidates at ``candidates/<sid>.json``; and the
harvested inventory run its passages were cut from at ``inputs/inventory/<run>/``. It
never carries a rendered view: the contributor renders the bytes with the frozen
renderer and checks every sha before any paid call (``work.render_sources``). Refetching
was the old way, and an old revision expands templates as they are now: two of the three
refetched in the Mac check of 2026-11-07 rendered another layer than the pinned one,
while the retained bytes re-rendered exactly (docs/CONTRIBUTE-MAC-CHECKS.md). The
publisher ships them only when it is given them (``build``'s ``sources_dir``,
``candidates_dir`` and ``inventory_dir``), and it renders each source and checks it
against the plan's pins first, so a package whose bytes do not reproduce its own pins
is refused on the coordinator's machine rather than after a contributor's paid call.

**No path of the coordinator's machine travels.** A plan for a contributor host roots
every path it writes at :data:`ROOT_PLACEHOLDER`; :func:`machine_paths` is the check
that nothing slipped through, and a package that still names a directory is a refusal,
never a rewrite. A shebang and a cache path under ``~/.cache`` are the same on every
machine, so the check is the three roots a package must not name, not "an absolute
path": a check that refuses a shebang teaches nobody anything. One file is exempt: a
shipped inventory's ``RECEIPT.json`` records where the coordinator harvested it, and
it travels byte for byte because the tally pins its sha.

**What a plan is, is stage 9c's, and stage 9c is TODO.** This module is the container:
it takes a planned-run directory as it stands, checks it, and writes the manifest. When
the planner lands, it writes that directory and nothing here changes.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
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
#: What a package may never carry, whatever directory it sits in: the article's rendered
#: views, and its fetched bytes anywhere but :func:`raw_path`.
REFUSED_NAMES = ("layer.txt", "body.html", "source.raw.html", "text.txt", "head.txt",
                 "reading.txt", "layer_map.json", "sections.json", "blocks.json",
                 "candidates.jsonl", "formulas.json", "paragraphs.json", "finders.json",
                 "render.json")
#: Where a shipped source's fetched bytes and its candidates sit: under the plan's own
#: ``sources/`` and ``candidates/``, which ``gate.json`` names as ``../sources`` and
#: ``../candidates``, so materialising the package lands each file where the gate and
#: staging read it.
SOURCES = "sources"
CANDIDATES = "candidates"
RAW_FILE = "source.raw.html"
#: Where the harvested inventory run travels, under :data:`INPUTS`: the seal tallies the
#: locate run against it (``seal.tally``), and nothing else of the plan reads it.
INVENTORY = "inventory"
#: What a package never carries at all: the declaration index travels as a bundle, and
#: the rest are what a run writes in its root (``work.MADE_DIRS``), which materialising
#: would otherwise overwrite with the publisher's copies.
NEVER_CARRIED = ("index", "work", "findings", "gate-log", "attempts", "usage")
#: One segment of a path a package may carry: nothing to escape, nothing hidden, no way
#: out of the package.
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


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
    """The names among ``paths`` that are article text, whatever directory they sit in.

    Compared casefolded: the default macOS disk is case-insensitive, so ``Layer.txt``
    lands where ``layer.txt`` would.
    """
    return sorted({p.name for p in paths if p.name.casefold() in REFUSED_NAMES})


def plain(rel: str) -> bool:
    """Whether ``rel`` is a relative path of plain segments, at most 300 characters."""
    return (isinstance(rel, str) and 0 < len(rel) <= 300
            and all(SEGMENT.fullmatch(part) for part in rel.split("/")))


def raw_path(source_id: str) -> str:
    """Where a package carries one source's fetched bytes."""
    return f"{SOURCES}/{source_id}/{RAW_FILE}"


def candidates_path(source_id: str) -> str:
    """Where a package carries one source's candidates."""
    return f"{CANDIDATES}/{source_id}.json"


def render_fetched(source_id: str, raw: bytes, *pins: dict) -> tuple[Any, list[str]]:
    """``(rendering, problems)``: one source's fetched bytes through the frozen renderer.

    Every ``pin`` is held to the rendering: its ``html_sha256``, ``layer_sha256`` and
    ``renderer`` where it records them, and the view shas under its ``views`` (a
    ``gate.json`` source pin's). Nothing is written, so a caller that finds a problem
    leaves no view on disk; ``rendering`` is None when the bytes do not render at all.
    """
    import render_web  # noqa: PLC0415 — only a package that ships bytes renders anything

    try:
        rendering = render_web.render(render_web.canonical_body(raw))
    except render_web.RenderError as exc:
        return None, [f"{source_id}: the fetched bytes do not render ({exc})"]
    got = {"html_sha256": rendering.html_sha256, "layer_sha256": rendering.layer_sha256,
           "renderer": render_web.RENDERER}
    views = rendering.record()["views"]
    problems: list[str] = []
    for pin in pins:
        for field, value in got.items():
            want = pin.get(field)
            if want is not None and want != value:
                problems.append(f"{source_id}: {field} renders to {str(value)[:12]} and the "
                                f"plan pins {str(want)[:12]}")
        for name, want in sorted((pin.get("views") or {}).items()):
            if want and views.get(name) != want:
                problems.append(f"{source_id}: {name} renders to other bytes than the plan "
                                "pins")
    return rendering, list(dict.fromkeys(problems))


#: The planned root's staged working directories. They hold the coordinator's own
#: copy of the article text (``run/workdir.py``), so they never travel: a package
#: carries the plan, and the contributor stages its own from its own rendering of the
#: sources. Everything else of the root does travel.
NOT_PACKAGED = ("work",)


def _files(root: Path) -> list[Path]:
    root = Path(root)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts
                  and p.relative_to(root).parts[0] not in NOT_PACKAGED)


def plan_json(plan_dir: Path, name: str) -> dict:
    path = Path(plan_dir) / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageProblem(f"the plan's {name} cannot be read ({exc})") from None
    if not isinstance(value, dict):
        raise PackageProblem(f"the plan's {name} is not a JSON object")
    return value


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _ship_sources(target: Path, plan_dir: Path, sources: list[dict], sources_dir: Path,
                  candidates_dir: Path | None) -> None:
    """Copy each pinned source's fetched bytes, and its candidates, into the package.

    By name, never by glob: ``<sources_dir>/<sid>/source.raw.html`` and
    ``<candidates_dir>/<sid>.json`` for every source the plan pins. Each source is
    rendered first and held to its sourceset entry and the gate's pin, and each
    candidates file to its source's rendering and the gate's index, so a stale byte is
    found here and not after a contributor's paid call.
    """
    gate = plan_json(plan_dir, "gate/gate.json")
    work = plan_json(plan_dir, "run.json").get("work") if (plan_dir / "run.json").is_file() \
        else None
    work = work if isinstance(work, dict) else {}
    pins = gate.get("sources") if isinstance(gate.get("sources"), dict) else {}
    entries = {str(entry.get("source_id")): entry for entry in sources}
    if set(pins) != set(entries):
        raise PackageProblem(
            f"gate.json pins {len(pins)} source(s) and the sourceset entries name "
            f"{len(entries)}, not the same ones; a package ships the plan's own sources")
    if gate.get("sources_dir") != f"../{SOURCES}":
        raise PackageProblem(
            f"gate.json reads the sources from {gate.get('sources_dir')!r}; a package ships "
            f"them at {SOURCES}/, so the plan must name ../{SOURCES} (plan.py's sources_as)")
    reads = (bool(work.get("candidates")) if "candidates" in work
             else bool(gate.get("candidates_dir")))
    if reads and gate.get("candidates_dir") != f"../{CANDIDATES}":
        raise PackageProblem(
            f"gate.json reads the candidates from {gate.get('candidates_dir')!r}; a package "
            f"ships them at {CANDIDATES}/, so the plan must name ../{CANDIDATES}")
    if reads and candidates_dir is None:
        raise PackageProblem("this run reads candidates, and a package that ships its "
                             "sources ships their candidates too")
    if not reads and candidates_dir is not None:
        raise PackageProblem("this run reads no candidates, so there are none to ship")
    index = gate.get("index") if isinstance(gate.get("index"), dict) else {}
    problems: list[str] = []
    for sid in sorted(entries):
        entry = entries[sid]
        pin = pins[sid] if isinstance(pins[sid], dict) else {}
        path = Path(sources_dir) / sid / RAW_FILE
        try:
            raw = path.read_bytes()
        except OSError:
            problems.append(f"{sid}: no {RAW_FILE} at {path}")
            continue
        if entry.get("raw_sha256") and keys.sha256_bytes(raw) != entry["raw_sha256"]:
            problems.append(f"{sid}: {path} is not the bytes the sourceset recorded")
            continue
        _rendering, found = render_fetched(sid, raw, entry, pin)
        problems.extend(found)
        _write(target / raw_path(sid), raw)
        if reads:
            problems.extend(_ship_candidates(target, sid, Path(candidates_dir), entry,
                                             index.get("sha256")))
    if problems:
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        raise PackageProblem("the shipped sources do not reproduce the plan's pins: "
                             + "; ".join(problems[:5]) + more)


def _ship_candidates(target: Path, sid: str, candidates_dir: Path, entry: dict,
                     index_sha256: str | None) -> list[str]:
    path = candidates_dir / f"{sid}.json"
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return [f"{sid}: no readable candidates at {path} ({type(exc).__name__})"]
    if not isinstance(value, dict):
        return [f"{sid}: the candidates at {path} are not a JSON object"]
    index = value.get("index") if isinstance(value.get("index"), dict) else {}
    problems = []
    if value.get("source_id") != sid:
        problems.append(f"{sid}: the candidates at {path} name source "
                        f"{str(value.get('source_id'))[:40]!r}")
    if value.get("source_sha") != entry.get("html_sha256"):
        problems.append(f"{sid}: the candidates were made from another rendering of the "
                        "source than the one pinned")
    if index_sha256 and index.get("sha256") != index_sha256:
        problems.append(f"{sid}: the candidates were made from another declaration index "
                        "than the one the gate pins")
    if not problems:
        _write(target / candidates_path(sid), data)
    return problems


def _ship_inventory(target: Path, plan_dir: Path, inventory_dir: Path,
                    sources: list[dict]) -> str:
    """Copy the harvested inventory run the plan was cut from; returns its run id.

    It must be a complete, gated harvest of the plan's own inventory run, and every
    document in it must be of a source as the sourceset pins it (its html, layer and
    renderer): a locate run is tallied against the inventory it was cut from, and an
    inventory of another rendering would fail the contributor's seal after a paid run.
    It travels byte for byte, its receipt included: the tally records the receipt's sha
    (``corpus.gate_summary``), and a seal over a rewritten receipt would vouch for a
    harvest nobody made.
    """
    import corpus as corpus_mod  # noqa: PLC0415 — only a package that ships one reads it

    locate = plan_json(plan_dir, "run.json").get("locate") or {}
    run = str(locate.get("inventory_run") or "") if isinstance(locate, dict) else ""
    if not run:
        raise PackageProblem("the plan names no inventory run (run.json's "
                             "locate.inventory_run), so there is no inventory to ship")
    inventory_dir = Path(inventory_dir)
    try:
        receipt = corpus_mod.require_gated(inventory_dir, run, "the inventory run")
    except corpus_mod.CorpusProblem as exc:
        raise PackageProblem(str(exc)) from None
    if receipt.get("run_id") not in (None, run):
        raise PackageProblem(f"{inventory_dir} is the harvest of {receipt.get('run_id')!r}, "
                             f"and the plan was cut from {run!r}")
    entries = {str(entry.get("source_id")): entry for entry in sources}
    problems: list[str] = []
    for path in _files(inventory_dir):
        rel = path.relative_to(inventory_dir)
        data = path.read_bytes()
        if path.suffix == ".json" and rel.as_posix() != corpus_mod.RECEIPT_NAME:
            try:
                document = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                document = None
            entry = entries.get(str((document or {}).get("source_id"))) \
                if isinstance(document, dict) else None
            for field, pinned in (("source_sha", "html_sha256"),
                                  ("layer_sha256", "layer_sha256"), ("renderer", "renderer")):
                if entry is not None and entry.get(pinned) is not None \
                        and document.get(field) != entry.get(pinned):
                    problems.append(f"{rel}: its {field} is not the source's pinned "
                                    f"{pinned}")
        _write(target / INPUTS / INVENTORY / run / rel, data)
    if problems:
        raise PackageProblem("the inventory was cut from other renderings than the plan "
                             "pins: " + "; ".join(problems[:5]))
    return run


def build(plan_dir: Path, *, run_id: str, repo: Path, sources: list[dict],
          universe: dict, mode: str = "locate", tasks: list[str] | None = None,
          max_cost: float | None = None, producer_commit: str | None = None,
          corpus_sha256: str | None = None, inputs: list[Path] | None = None,
          recorded_argv: list[str] | None = None,
          extra_roots: list[str] | None = None, sources_dir: Path | None = None,
          candidates_dir: Path | None = None, inventory_dir: Path | None = None,
          index_bundle: str | None = None, seal: dict | None = None,
          created: str | None = None) -> dict:
    """Copy a planned run into ``packages/<run-id>/`` and write its manifest.

    ``plan_dir`` is the planner's output directory, rooted at :data:`ROOT_PLACEHOLDER`,
    less its staged working directories (:data:`NOT_PACKAGED`): those hold the
    coordinator's copy of the article text, and the contributor stages its own.
    ``sources`` are the sourceset entries; ``universe`` is the universe document.
    ``inputs`` are files a recorded command names, which travel under ``inputs/`` so the
    recorded argument can name them there.

    ``sources_dir`` (the sourceset's source directories), ``candidates_dir`` and
    ``inventory_dir`` (the inventory run's harvested corpus) ship the article's bytes,
    checked as :func:`_ship_sources` and :func:`_ship_inventory` say. ``index_bundle``
    (the sha256 of the index bundle's ``MANIFEST.json``) and ``seal`` (the campaign a
    contributor seals under) are what the claims API's packages record besides.
    """
    plan_dir = Path(plan_dir)
    if not plan_dir.is_dir():
        raise PackageProblem(f"{plan_dir} is not a planned-run directory")
    for name in ("run.json", "gate/gate.json"):
        planned = plan_json(plan_dir, name).get("run_id") if (plan_dir / name).is_file() \
            else None
        if planned is not None and planned != run_id:
            raise PackageProblem(f"the plan's {name} is run {planned!r}, and this package "
                                 f"would be {run_id!r}; a package is its plan's run")
    if candidates_dir is not None and sources_dir is None:
        raise PackageProblem("candidates ship with their sources' fetched bytes, or not "
                             "at all")
    if max_cost is not None and not (isinstance(max_cost, (int, float))
                                     and math.isfinite(max_cost) and max_cost >= 0):
        raise PackageProblem(f"a spend cap is a finite number of USD, not {max_cost!r}")
    target = package_dir(repo, run_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    staged = _files(plan_dir)
    named = refused_names(staged)
    if named:
        raise PackageProblem(
            f"the plan would carry article text ({', '.join(named)}); a package carries the "
            "plan, the sources' pins and the universe, and a source's fetched bytes only "
            "through sources_dir, which checks them")
    stray = sorted({p.relative_to(plan_dir).parts[0] for p in staged}
                   & {SOURCES, CANDIDATES, *NEVER_CARRIED})
    if stray:
        raise PackageProblem(
            f"the plan holds {', '.join(name + '/' for name in stray)}; a package ships a "
            "source's fetched bytes and candidates only through sources_dir and "
            "candidates_dir, which check them, and never carries index/ or work/")
    for path in staged:
        copy = target / path.relative_to(plan_dir)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    for path in inputs or []:
        if Path(path).name == INVENTORY:
            raise PackageProblem(f"an input named {INVENTORY!r} would sit where the "
                                 "inventory run travels")
        copy = target / INPUTS / Path(path).name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    if sources_dir is not None:
        _ship_sources(target, plan_dir, sources, Path(sources_dir),
                      None if candidates_dir is None else Path(candidates_dir))
    receipt = None
    if inventory_dir is not None:
        run = _ship_inventory(target, plan_dir, Path(inventory_dir), sources)
        receipt = (Path(INPUTS) / INVENTORY / run / "RECEIPT.json").as_posix()

    (target / SOURCES_JSONL).write_text(
        "".join(keys.canonical_line({key: entry.get(key) for key in vocab.SOURCE_KEYS})
                for entry in sources), encoding="utf-8")
    (target / UNIVERSE_JSON).write_text(keys.canonical_line(universe), encoding="utf-8")

    roots = machine_roots(extra_roots)
    for path in _files(target):
        if path.relative_to(target).as_posix() == receipt:
            # the harvest receipt records where the coordinator harvested it, and it
            # travels byte for byte because the tally pins its sha; nothing reads those paths
            continue
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
        "created": created or now(),
        "producer_commit": producer_commit,
        "max_cost": max_cost,
        "tasks": sorted(tasks or []),
        "sources": [{"source_id": entry["source_id"], "pageid": entry["pageid"],
                     "revision": entry["revision"], "raw_sha256": entry.get("raw_sha256"),
                     "html_sha256": entry.get("html_sha256"),
                     "layer_sha256": entry.get("layer_sha256"),
                     "renderer": entry.get("renderer")}
                    for entry in sorted(sources, key=lambda e: str(e.get("source_id")))],
        "universe": {"id": universe.get("id"),
                     "universe_sha256": universe.get("universe_sha256"),
                     "corpus_sha256": corpus_sha256},
        "recorded_argv": list(recorded_argv or []),
        "files": {},
    }
    if index_bundle is not None:
        manifest["index_bundle"] = {"manifest_sha256": index_bundle}
    if seal is not None:
        manifest["seal"] = dict(seal)
    manifest["files"] = {p.relative_to(target).as_posix(): keys.sha256_file(p)
                         for p in _files(target)}
    text = json.dumps(manifest, indent=1, sort_keys=True, allow_nan=False) + "\n"
    found = machine_paths(text, roots)
    if found:
        raise PackageProblem(f"{PACKAGE_JSON} would name this machine's {found[0]} (a recorded "
                             f"argument names {PACKAGE_PLACEHOLDER}/inputs/..., not a path "
                             "of the coordinator's)")
    (target / PACKAGE_JSON).write_text(text, encoding="utf-8")
    problems = check(target, manifest)
    if problems:
        raise PackageProblem(f"the package just written does not read back: "
                             + "; ".join(problems[:5]))
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
    # every file and link, work/ included and no link followed: materialising copies
    # whatever the directory holds, and a linked directory is a door out of the package
    for top, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in sorted(dirs + files):
            path = Path(top) / name
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink():
                problems.append(f"{rel}: a symlink")
            elif name in files and rel != PACKAGE_JSON and rel not in named:
                problems.append(f"{rel}: not in {PACKAGE_JSON}")
    problems.extend(shape(manifest))
    return list(dict.fromkeys(problems))


def shape(manifest: dict) -> list[str]:
    """Where a package's files may sit.

    The plan anywhere; a source's fetched bytes and its candidates only at
    :func:`raw_path` and :func:`candidates_path`, for every pinned source or for none;
    the article's rendered views nowhere; and nothing under ``index/`` or ``work/``.
    """
    named = dict(manifest.get("files") or {})
    listed = manifest.get("sources")
    sources = listed if isinstance(listed, list) else []
    pins = {str(pin.get("source_id")): pin for pin in sources if isinstance(pin, dict)}
    problems: list[str] = [f"{sid[:80]!r} is not a source id" for sid in sorted(pins)
                           if not vocab.SOURCE_ID_RE.match(sid)]
    if listed is not None and (not isinstance(listed, list) or len(pins) != len(listed)):
        problems.append(f"{PACKAGE_JSON}'s sources are not one pin per source")
    raw = {raw_path(sid): sid for sid in pins}
    candidates = {candidates_path(sid): sid for sid in pins}
    seen: dict[str, str] = {}
    for rel in sorted(named):
        if not plain(rel):
            problems.append(f"{rel[:120]!r}: not a plain path (letters, digits, . _ - in "
                            "each segment)")
            continue
        if rel.casefold() in seen:
            problems.append(f"{rel}: the same path as {seen[rel.casefold()]} on a "
                            "case-insensitive disk")
        seen[rel.casefold()] = rel
        # casefolded, because the default macOS disk puts Work/ where work/ is
        top = rel.split("/", 1)[0].casefold()
        if top in NEVER_CARRIED:
            problems.append(f"{rel}: a package never carries {top}/")
        elif top == SOURCES and rel not in raw:
            problems.append(f"{rel}: {SOURCES}/ carries only each pinned source's {RAW_FILE}")
        elif top == CANDIDATES and rel not in candidates:
            problems.append(f"{rel}: {CANDIDATES}/ carries only each pinned source's "
                            "<source id>.json")
    text = refused_names([Path(rel) for rel in named if rel not in raw])
    if text:
        problems.append(f"the package carries article text ({', '.join(text)})")
    for kind, paths in (("fetched bytes", raw), ("candidates", candidates)):
        shipped = [rel for rel in paths if rel in named]
        if shipped and len(shipped) != len(pins):
            problems.append(f"the package ships {kind} for {len(shipped)} of its "
                            f"{len(pins)} source(s)")
    for rel, sid in sorted(raw.items()):
        want = pins[sid].get("raw_sha256")
        if rel in named and want and named[rel] != want:
            problems.append(f"{rel}: not the bytes its source pin names")
    return problems
