#!/usr/bin/env python3
"""Decide whether ``main`` may move past a release's authority commit.

A frozen Brain release and its activation bundle name the Git commit they were built
from. Promotion and bundle freezing run from a clean ``main`` checkout, and ``main``
may have moved on since the build. The release stays usable only when every path that
differs between the authority commit and the checkout is *release-neutral*: text that
never reaches release, public-asset, or Worker bytes. Neutral paths are listed here
explicitly; everything else (reducer code and curated inputs, authority contracts and
schemas, Worker source and configuration, lockfiles, public asset sources, annotation
mirrors, non-README Markdown) is release-affecting by default, so a new file or
directory is never silently neutral. The checkout must also descend from the authority commit: ``main`` only moves
forward past a built release.

Paths use the repository-relative POSIX form that ``git diff --name-only`` prints.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import PurePosixPath

POLICY = "wikilean.release-neutral-paths/v1"

# Directories whose contents never feed release, public-asset, or Worker bytes.
NEUTRAL_DIRECTORIES: tuple[str, ...] = (
    ".claude",
    ".github",
    "bench",
    "bot",
    "brain/authority/fixtures",
    "docs",
    "manage",
    "scripts",
    "site/ops",
    "wiki/e2e",
    "wiki/test",
    "wikifunctions",
)
# Paths inside a neutral directory, or matching a neutral name rule, that the
# nightly's own release-affecting source check (site/ops/brain-nightly.sh) treats
# as release-affecting; the two policies must agree.
AFFECTING_EXCEPTIONS: tuple[str, ...] = (
    "site/ops/brain-nightly.sh",
    "site/ops/nightly.env",
    "site/test_frontier_page.py",
)
NEUTRAL_FILES: tuple[str, ...] = (
    ".gitattributes",
    ".gitignore",
    "CLAUDE.md",
    "HANDOFF.md",
    "LICENSE",
)
# Markdown is neutral only as a README or under a neutral directory; other
# Markdown (brain/SCHEMA.md, templates) may be read at build time.
NEUTRAL_NAMES: tuple[str, ...] = ("README.md",)

GIT_DIFF_ARGUMENTS: tuple[str, ...] = ("diff", "--no-renames", "--name-only", "-z")


def is_release_neutral(path: str) -> bool:
    """Return whether one repository-relative path is release-neutral."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    pure = PurePosixPath(path)
    if pure.as_posix() != path or any(part in {"", ".", ".."} for part in pure.parts):
        return False
    if path in AFFECTING_EXCEPTIONS:
        return False
    if path in NEUTRAL_FILES or pure.name in NEUTRAL_NAMES:
        return True
    if pure.name.startswith("test_") and pure.name.endswith(".py"):
        return True
    return any(path.startswith(directory + "/") for directory in NEUTRAL_DIRECTORIES)


def blocking_changes(paths: Iterable[str]) -> list[str]:
    """Return the sorted release-affecting subset of ``paths``."""
    return sorted({path for path in paths if not is_release_neutral(path)})


def changed_paths(run_git: Callable[[Sequence[str]], str], base: str, head: str) -> list[str]:
    """List every path that differs between two commits, renames shown as both sides.

    ``run_git`` receives the Git arguments (without the executable) and returns the
    command's standard output decoded as text.
    """
    output = run_git([*GIT_DIFF_ARGUMENTS, base, head])
    return sorted({path for path in output.split("\0") if path})


def validate_neutral_changes(value: object, label: str) -> list[str]:
    """Validate a recorded neutral-change list: sorted, unique, release-neutral paths."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of paths")
    if value != sorted(set(value)):
        raise ValueError(f"{label} must be sorted and unique")
    blocking = blocking_changes(value)
    if blocking:
        raise ValueError(f"{label} names release-affecting paths: {', '.join(blocking[:20])}")
    return list(value)


def describe_blocking(authority_commit: str, head: str, blocking: Sequence[str]) -> str:
    shown = ", ".join(blocking[:20])
    more = f" (+{len(blocking) - 20} more)" if len(blocking) > 20 else ""
    return (
        f"main {head} changed release-affecting paths since release authority "
        f"{authority_commit}: {shown}{more}"
    )
