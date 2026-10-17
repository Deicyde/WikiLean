#!/usr/bin/env python3
"""Fail when a tracked test_*.py is neither run by scripts/ci-python.sh nor corpus-only.

A test file that no gate runs stops being a test. Every tracked ``test_*.py`` must be
named by a ``run_check`` line in ci-python.sh (as a script path, or as a module after
``-m unittest``) or be listed in CORPUS_ONLY with what it needs that a fresh checkout
lacks. A CORPUS_ONLY entry that is no longer tracked, or that ci-python.sh now runs, is
also an error, so the allowlist cannot drift from the tree.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
CI_SCRIPT = ROOT / "scripts" / "ci-python.sh"

# Brain acceptance gates that need inputs a fresh checkout lacks. The Brain nightly
# runs the first four against its build; test_fold_xref is a local corpus check
# (CONTRIBUTING.md, "Corpus-dependent checks").
CORPUS_ONLY: dict[str, str] = {
    "brain/test_acceptance.py": "the gitignored brain/data/edges_links.jsonl from a Brain build",
    "brain/test_cells.py": "numpy for its layout check (brain/layout.py), which requirements-ci.txt does not pin",
    "brain/test_cell_shards.py": "the gitignored site/assets/brain/cells/ from a Brain build",
    "brain/test_frontier.py": "the gitignored brain/data/synapses.jsonl from a Brain build",
    "brain/test_fold_xref.py": "the private declaration cache and a Mathlib checkout",
}


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") and name.endswith(".py")


def tracked_tests() -> set[str]:
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {path for path in output.split("\0") if path and is_test_file(path)}


def ci_run_tests(script: str) -> set[str]:
    """Return the test files that ``run_check`` lines execute, as repository paths."""
    run: set[str] = set()
    for line in script.splitlines():
        if not line.startswith("run_check "):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError as exc:
            raise SystemExit(f"cannot parse scripts/ci-python.sh line {line!r}: {exc}") from exc
        for index, word in enumerate(words):
            if is_test_file(word):
                run.add(word)
            elif word == "unittest" and index > 0 and words[index - 1] == "-m":
                for module in words[index + 1:]:
                    if module.startswith("-"):
                        continue
                    candidate = module.replace(".", "/") + ".py"
                    if is_test_file(candidate):
                        run.add(candidate)
    return run


def problems(tracked: set[str], run: set[str]) -> list[str]:
    found = [
        f"{path}: tracked but not run by scripts/ci-python.sh; add a run_check line "
        "or, if it needs a local corpus, list it in CORPUS_ONLY"
        for path in sorted(tracked - run - CORPUS_ONLY.keys())
    ]
    found += [f"{path}: run by scripts/ci-python.sh but not tracked" for path in sorted(run - tracked)]
    found += [f"{path}: in CORPUS_ONLY but not tracked" for path in sorted(CORPUS_ONLY.keys() - tracked)]
    found += [
        f"{path}: in CORPUS_ONLY but scripts/ci-python.sh runs it"
        for path in sorted(CORPUS_ONLY.keys() & run)
    ]
    return found


def main() -> int:
    tracked = tracked_tests()
    run = ci_run_tests(CI_SCRIPT.read_text(encoding="utf-8"))
    found = problems(tracked, run)
    if not tracked or not run:
        found.append("found no tracked or no CI-run test files; refusing to pass vacuously")
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"{len(tracked)} tracked test files: {len(run)} run by CI, {len(CORPUS_ONLY)} corpus-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
