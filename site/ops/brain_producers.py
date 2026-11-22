#!/usr/bin/env python3
"""Run a Brain producer on the operator Mac, and stage its export for the Brain build.

    brain_producers.py run   <producer> --repo REPO --store STORE [--python PY] [--keep N]
    brain_producers.py stage <producer> --store STORE --into DIR
    brain_producers.py check <producer> --store STORE --max-age-hours H

`site/ops/brain-nightly.sh` calls `run` with its weekly sources and `stage` and `check` every
night, all fail-soft (docs/BRAIN-PLANS.md, "Running the producer in the nightly").

`run`, for one producer:
  1. Refuse producer code that differs from the checkout's HEAD: an export names the commit
     that built it, so that commit must be the code.
  2. `fetch` (the producer's only networked step) into STORE/inputs/<producer>/, then the
     offline `build` into a fresh STORE/work/<producer>/out, with an interpreter that must be
     CPython 3.12.
  3. Publish only a new logical_root: STORE/exports/<producer>/<root>/ (the export plus
     PRODUCER.json and PINS.json, read-only), and move `current` to it.
  4. Keep the newest N exports, `current`, and every root listed in PINNED.
  5. Record last_success.json or last_failure.json, so freshness is measurable.

`stage` copies exactly the export's own files from `current` into DIR (brain/data/<producer>)
when its root differs from the staged one: verified with the Brain's own reader
(brain/plans_layer.py) in a sibling folder first, then swapped in. On any failure the staged
export stays as it was.

STORE must be outside every git work tree: the producers refuse an --out inside one, and an
export is never committed. Nothing here touches Cloudflare, GitHub credentials or anything
outside STORE and DIR.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain"))

import plans_layer  # noqa: E402

PRODUCERS = {
    "tauceti_plans": {
        "module": "scripts.tauceti_plans",
        "code": ("scripts/tauceti_plans", "scripts/brain_producers"),
        "files": (*plans_layer.EXPORT_FILES, plans_layer.MANIFEST),
        "load": plans_layer.load_export,
        "validate": plans_layer.validate_export,
    },
}


def verified(spec: dict, directory: Path):
    """The export at `directory` if it passes every check the Brain build makes, else raise.

    None when nothing is there. The build's own refusals (load_export, then build_layer's
    structure checks) run here first, so an export that would refuse the build is never
    published or staged.
    """
    export = spec["load"](directory)
    if export is not None:
        spec["validate"](export)
    return export
DEFAULT_STORE = Path.home() / ".local" / "share" / "wikilean" / "brain-producers"
DEFAULT_KEEP = 8


class ProducerError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def git_work_tree_holding(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _make_writable(tree: Path) -> None:
    for dirpath, _dirs, files in os.walk(tree):
        os.chmod(dirpath, 0o755)
        for name in files:
            p = Path(dirpath) / name
            if not p.is_symlink():
                os.chmod(p, 0o644)


def _seal(tree: Path) -> None:
    for dirpath, _dirs, files in os.walk(tree, topdown=False):
        for name in files:
            p = Path(dirpath) / name
            if not p.is_symlink():
                os.chmod(p, 0o444)
        os.chmod(dirpath, 0o555)


def _paths(store: Path, producer: str) -> dict[str, Path]:
    return {
        "inputs": store / "inputs" / producer,
        "exports": store / "exports" / producer,
        "work": store / "work" / producer,
        "lock": store / f"{producer}.lock",
    }


def _run(cmd: list[str], *, cwd: Path, stdout: Path, stage: str) -> None:
    with stdout.open("wb") as out:
        proc = subprocess.run(cmd, cwd=cwd, stdout=out, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise ProducerError(stage, f"{' '.join(cmd[5:7])} exited {proc.returncode}")


def _interpreter_version(python: str) -> str:
    proc = subprocess.run(
        [python, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProducerError("interpreter", f"{python} does not run")
    version = proc.stdout.strip()
    if not version.startswith("3.12."):
        raise ProducerError("interpreter", f"{python} is Python {version}; producers need 3.12")
    return version


def _code_commit(repo: Path, code: tuple[str, ...]) -> str:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise ProducerError("checkout", f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout
    commit = git("rev-parse", "HEAD").strip()
    dirty = git("status", "--porcelain", "--untracked-files=all", "--", *code).strip()
    if dirty:
        raise ProducerError("checkout", "producer code differs from HEAD; an export must name "
                                        "the commit that built it:\n" + dirty)
    return commit


def _retain(exports: Path, keep: int) -> list[str]:
    current = os.readlink(exports / "current") if (exports / "current").is_symlink() else None
    pinned_file = exports / "PINNED"
    pinned = set(pinned_file.read_text().split()) if pinned_file.exists() else set()
    roots = [p for p in exports.iterdir()
             if len(p.name) == 64 and all(c in "0123456789abcdef" for c in p.name)
             and p.is_dir() and not p.is_symlink()]
    roots.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    pruned = []
    for old in roots[keep:]:
        if old.name != current and old.name not in pinned:
            _make_writable(old)
            shutil.rmtree(old)
            pruned.append(old.name)
    return pruned


def run(producer: str, *, repo: Path, store: Path, python: str, keep: int) -> int:
    spec = PRODUCERS[producer]
    store = store.resolve()
    if git_work_tree_holding(store) is not None:
        print(f"brain_producers: {store} is inside a git work tree; refusing", file=sys.stderr)
        return 2
    p = _paths(store, producer)
    for key in ("inputs", "exports", "work"):
        p[key].parent.mkdir(parents=True, exist_ok=True)
    p["exports"].mkdir(exist_ok=True)
    lock = p["lock"].open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"brain_producers: another {producer} run holds {p['lock']}", file=sys.stderr)
        return 1
    stage = "start"
    try:
        stage = "interpreter"
        version = _interpreter_version(python)
        stage = "checkout"
        commit = _code_commit(repo, spec["code"])

        stage = "workspace"
        if p["work"].exists():
            _make_writable(p["work"])
            shutil.rmtree(p["work"])
        p["work"].mkdir()
        out = p["work"] / "out"
        # -B: the run writes nothing into the checkout (no __pycache__ beside the code).
        py = [python, "-B", "-E", "-s", "-m", spec["module"]]
        stage = "fetch"
        _run([*py, "fetch", "--inputs", str(p["inputs"])], cwd=repo,
             stdout=p["work"] / "fetch.json", stage=stage)
        stage = "build"
        _run([*py, "build", "--inputs", str(p["inputs"]), "--out", str(out)], cwd=repo,
             stdout=p["work"] / "build.json", stage=stage)

        stage = "verify"
        try:
            export = verified(spec, out)
        except plans_layer.PlansExportError as exc:
            raise ProducerError(stage, f"the new export does not verify: {exc}") from exc
        if export is None:
            raise ProducerError(stage, "the build wrote no export")
        root = export.logical_root

        stage = "publish"
        target = p["exports"] / root
        published = False
        if target.exists():
            print(f"brain_producers: {producer} unchanged at {root[:12]}")
        else:
            _write_json_atomic(out / "PRODUCER.json", {
                "producer": producer, "code_commit": commit, "logical_root": root,
                "python": version})
            shutil.copyfile(p["inputs"] / "PINS.json", out / "PINS.json")
            os.rename(out, target)
            _seal(target)
            published = True
            print(f"brain_producers: {producer} published {root[:12]} (code {commit[:12]})")
        link_tmp = p["exports"] / ".current.tmp"
        link_tmp.unlink(missing_ok=True)
        os.symlink(root, link_tmp)
        os.replace(link_tmp, p["exports"] / "current")

        stage = "retention"
        for name in _retain(p["exports"], keep):
            print(f"brain_producers: pruned {name[:12]}")

        stage = "record"
        _write_json_atomic(p["exports"] / "last_success.json", {
            "utc": utc_now(), "root": root, "code_commit": commit, "python": version,
            "published": published})
        return 0
    except Exception as exc:  # recorded, then fail-soft: the nightly keeps the staged export
        stage = getattr(exc, "stage", stage)
        try:
            _write_json_atomic(p["exports"] / "last_failure.json", {
                "utc": utc_now(), "stage": stage, "error": str(exc)[:2000]})
        except OSError:
            pass
        print(f"brain_producers: {producer} FAILED at {stage}: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.close()


def _quarantine(into: Path, reason: str) -> None:
    """Move an invalid staged export aside (keeping only the newest such copy), so the build
    goes on without the plans layer instead of refusing the whole nightly."""
    for old in into.parent.glob(f".{into.name}.invalid-*"):
        shutil.rmtree(old, ignore_errors=True)
    target = into.parent / f".{into.name}.invalid-{utc_now().replace(':', '')}"
    os.rename(into, target)
    print(f"brain_producers: the staged export at {into} is invalid ({reason}); moved it to "
          f"{target.name} and nothing is staged", file=sys.stderr)


def stage_export(producer: str, *, store: Path, into: Path) -> int:
    spec = PRODUCERS[producer]
    exports = _paths(store.resolve(), producer)["exports"]
    into.parent.mkdir(parents=True, exist_ok=True)
    # Leftovers of a stage that was killed between its two renames.
    for pattern in (f".{into.name}.staging-*", f".{into.name}.old-*"):
        for leftover in into.parent.glob(pattern):
            shutil.rmtree(leftover, ignore_errors=True)

    staged_root, staged_error = None, None
    try:
        staged = verified(spec, into)
        staged_root = staged.logical_root if staged else None
    except plans_layer.PlansExportError as exc:
        staged_error = str(exc)

    current = exports / "current"
    source = exports / os.readlink(current) if current.is_symlink() else None
    if source is not None and staged_root == source.name:
        print(f"brain_producers: {producer} {staged_root[:12]} already staged")
        return 0
    status = 0
    if source is not None:
        tmp = Path(tempfile.mkdtemp(prefix=f".{into.name}.staging-", dir=into.parent))
        try:
            for name in spec["files"]:
                shutil.copyfile(source / name, tmp / name)
            try:
                export = verified(spec, tmp)
                if export is None or export.logical_root != source.name:
                    raise plans_layer.PlansExportError("not the root its folder names")
            except plans_layer.PlansExportError as exc:
                print(f"brain_producers: {producer} export {source.name[:12]} does not verify "
                      f"({exc}); it is not staged", file=sys.stderr)
                status = 1
            else:
                os.chmod(tmp, 0o755)
                old = None
                if into.exists() or into.is_symlink():
                    old = Path(tempfile.mkdtemp(prefix=f".{into.name}.old-", dir=into.parent))
                    os.rename(into, old / into.name)
                os.rename(tmp, into)
                if old is not None:
                    shutil.rmtree(old, ignore_errors=True)
                print(f"brain_producers: staged {producer} {source.name[:12]} at {into}")
                return 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"brain_producers: no {producer} export yet; the staged one (if any) is kept")
    if staged_error is not None:
        _quarantine(into, staged_error)
        return 1
    return status


def _epoch(utc: str | None) -> float | None:
    if not utc:
        return None
    try:
        return datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def check(producer: str, *, store: Path, max_age_hours: float, now: float | None = None) -> int:
    exports = _paths(store.resolve(), producer)["exports"]

    def record(name: str) -> dict | None:
        try:
            return json.loads((exports / name).read_text())
        except (OSError, json.JSONDecodeError):
            return None
    success, failure = record("last_success.json"), record("last_failure.json")
    print(json.dumps({"producer": producer, "last_success": success, "last_failure": failure},
                     sort_keys=True))
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    s_at = _epoch((success or {}).get("utc"))
    f_at = _epoch((failure or {}).get("utc"))
    if s_at is None:
        print(f"STALE: {producer} has never succeeded", file=sys.stderr)
        return 1
    age_h = (now - s_at) / 3600
    if age_h > max_age_hours:
        print(f"STALE: {producer} last succeeded {age_h:.0f}h ago "
              f"(limit {max_age_hours:.0f}h)", file=sys.stderr)
        return 1
    if f_at is not None and f_at > s_at:
        print(f"FAILING: {producer} failed at {failure['utc']} ({failure.get('stage')}), "
              f"after its last success", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "stage", "check"):
        s = sub.add_parser(name)
        s.add_argument("producer", choices=sorted(PRODUCERS))
        s.add_argument("--store", type=Path, default=DEFAULT_STORE)
        if name == "run":
            s.add_argument("--repo", type=Path, default=ROOT)
            s.add_argument("--python", default=sys.executable)
            s.add_argument("--keep", type=int, default=DEFAULT_KEEP)
        elif name == "stage":
            s.add_argument("--into", type=Path, required=True)
        else:
            s.add_argument("--max-age-hours", type=float, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.producer, repo=args.repo.resolve(), store=args.store,
                   python=args.python, keep=max(1, args.keep))
    if args.command == "stage":
        return stage_export(args.producer, store=args.store, into=args.into)
    return check(args.producer, store=args.store, max_age_hours=args.max_age_hours)


if __name__ == "__main__":
    raise SystemExit(main())
