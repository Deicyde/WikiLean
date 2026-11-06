#!/usr/bin/env python3
"""The coordinator's side of contribute mode: publish one planned run as a package.

docs/PIPELINE.md section 3 (stage 11). Copies a planned locate run into the work repo's
``packages/<run-id>/``, writes its manifest and commits it::

    python3 scripts/pipeline/contribute/publish_package.py --run-id loc-7 \\
        --plan <planned-run dir> --sources <sources.jsonl> --universe <universe.json> \\
        --repo <work-repo clone> [--mode locate] [--max-cost 20] [--no-commit]

The plan's paths are rooted at ``{{WIKILEAN_CONTRIB_ROOT}}``; a package that still names
a directory of this machine is refused, never rewritten. No article text travels: the
contributor refetches each page at its pinned revision (``package.py``).

Then push the branch, and upload the declaration index once per universe as a release
asset of the work repo — it is hundreds of megabytes and does not belong in git. That
upload is the coordinator's own decision, so no command here does it.

Exit codes: 0 published, 2 the package was refused, 3 the publisher failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import package as package_mod  # noqa: E402
import workrepo  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def publish(*, run_id: str, plan: Path, sources: Path, universe: Path, repo: Path,
            mode: str = "locate", max_cost: float | None = None,
            producer_commit: str | None = None, corpus_sha256: str | None = None,
            tasks: list[str] | None = None, inputs: list[Path] | None = None,
            recorded_argv: list[str] | None = None, commit: bool = True) -> dict:
    """Write and (by default) commit the package; returns its manifest."""
    clone = workrepo.check_clone(repo)
    manifest = package_mod.build(
        Path(plan), run_id=run_id, repo=clone,
        sources=read_jsonl(sources),
        universe=json.loads(Path(universe).read_text(encoding="utf-8")),
        mode=mode, tasks=tasks, max_cost=max_cost, producer_commit=producer_commit,
        corpus_sha256=corpus_sha256, inputs=inputs, recorded_argv=recorded_argv,
        extra_roots=[str(Path(plan).resolve().parent)])
    rel = f"{workrepo.PACKAGES}/{run_id}"
    if commit:
        head = workrepo.commit_paths(
            clone, [rel],
            f"contribute: package locate run {run_id} ({mode}, "
            f"{len(manifest['tasks'])} task(s))")
        manifest = {**manifest, "commit": head}
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", required=True, type=Path,
                        help="the planner's output directory, rooted at the placeholder")
    parser.add_argument("--sources", required=True, type=Path,
                        help="the sourceset entries, as JSONL (pins only, no bytes)")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path, help="a clone of the work repo")
    parser.add_argument("--mode", default="locate")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="the spend cap recorded for this package, in USD")
    parser.add_argument("--producer-commit", default=None)
    parser.add_argument("--corpus-sha256", default=None,
                        help="the sha256 of the declaration index the contributor downloads")
    parser.add_argument("--task", dest="tasks", action="append", default=[])
    parser.add_argument("--input", dest="inputs", action="append", default=[], type=Path,
                        help="a file a recorded command names; it travels under inputs/")
    parser.add_argument("--recorded-arg", dest="recorded_argv", action="append", default=[],
                        help="one token of the command the contributor re-runs (repeatable)")
    parser.add_argument("--no-commit", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = publish(run_id=args.run_id, plan=args.plan, sources=args.sources,
                           universe=args.universe, repo=args.repo, mode=args.mode,
                           max_cost=args.max_cost, producer_commit=args.producer_commit,
                           corpus_sha256=args.corpus_sha256, tasks=args.tasks,
                           inputs=args.inputs, recorded_argv=args.recorded_argv,
                           commit=not args.no_commit)
    except (package_mod.PackageProblem, workrepo.WorkRepoProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"publishing failed: {exc!r}", file=sys.stderr)
        return 3
    print(f"packaged {manifest['run_id']} ({manifest['mode']}): "
          f"{len(manifest['sources'])} source(s) pinned by revision and sha, "
          f"{len(manifest['tasks'])} task(s), {len(manifest['files'])} file(s)")
    print(f"  universe : {manifest['universe']['id']} "
          f"{str(manifest['universe']['universe_sha256'])[:12]}")
    if "commit" in manifest:
        print(f"  committed: {manifest['commit'][:12]}; push it, then upload the declaration "
              "index once per universe as a release asset of the work repo")
    else:
        print("  not committed (--no-commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
