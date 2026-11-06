#!/usr/bin/env python3
"""The Claude Code sandbox a contributor machine's agents run under.

docs/PIPELINE.md section 3 (stage 11), invariants 2 and 3. A run host confines its
agents with its own Linux sandbox; a contributor machine is a Mac, so the confinement is
Claude Code's own, turned on by a settings file the bridge passes to
``claude --settings``.

**One function writes it** (:func:`settings_json`), so the shape that must be checked on
a Mac is in one place, and it is written **per agent call**: the staged file names the
task worktree by :data:`WORKTREE_PLACEHOLDER` and ``bridge.sh`` substitutes the working
directory the orchestrator ran it in, so each call is confined to its own worktree
instead of to the whole entrypoint repository the worktrees sit under. A file written at
stage time could only name that repository, and a worker for one task could then write
into another task's worktree.

The keys were read out of a Claude Code binary (2.1.311):

* ``sandbox.enabled`` — the sandbox itself; the CLI's own refusal text names this key;
* ``sandbox.filesystem.disabled`` — ``false`` keeps the filesystem half on;
* ``sandbox.filesystem.allowWrite`` / ``denyWrite`` — extra writable paths, and paths
  denied inside them;
* ``sandbox.autoAllowBashIfSandboxed`` — ``false`` keeps Bash prompting where the
  allowlist does not already admit a command; a sandboxed command is not an allowed one.

**The macOS path fix.** The sandbox matches the *real* path a write lands on, and on
macOS ``/var`` and ``/tmp`` are symlinks into ``/private``. So every root but the
placeholder goes through ``os.path.realpath`` here, and ``bridge.sh`` names the call's
own worktree by ``pwd -P``.

**What only a real Mac can prove** is docs/PIPELINE.md section 9: that these are the
keys this build reads, that a ``--settings`` file's ``allowWrite`` is honoured at all,
that macOS's backend still lets a worker write its document and run ``git`` in its
worktree, and whether a call needs to write Claude Code's own state under
``$HOME/.claude``, which no root here admits. Nothing on Linux can answer any of them,
and a build that renamed a key would leave the agents unsandboxed with only the prompt
stopping them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: The file the staged wrappers point ``claude --settings`` at, under the run's root.
SETTINGS_NAME = "claude-settings.json"
#: The variable the wrappers export, which ``bridge.sh`` reads.
SETTINGS_ENV = "WIKILEAN_CLAUDE_SETTINGS"
#: The task worktree, which only the call knows. The orchestrator gives each agent call
#: its own worktree and runs the wrapper in it, so a settings file written at stage time
#: cannot name it. The spelling is shared with ``bridge.sh`` and the tests pin the two.
WORKTREE_PLACEHOLDER = "{{WIKILEAN_TASK_WORKTREE}}"
#: Where the bridge writes the call's own settings file: inside the worktree's own
#: ``.tend/``, which the entrypoint's git excludes, so it is no part of any change set.
CALL_SETTINGS_RELATIVE = ".tend/claude-settings.json"
#: The exit code the bridge uses for "I could not confine this call": an infrastructure
#: failure the orchestrator retries, never a call run unsandboxed.
UNSANDBOXED_EXIT = 78


def globs(paths: list[str]) -> list[str]:
    """Each path as a ``<path>/**`` glob, by its real name, each written once."""
    out: list[str] = []
    for path in paths:
        # the real path, because that is where the agent's writes land: on macOS /var and
        # /tmp are symlinks into /private, and the bridge names the worktree by `pwd -P`
        resolved = path if str(path) == WORKTREE_PLACEHOLDER else os.path.realpath(path)
        glob = str(resolved).rstrip("/") + "/**"
        if glob not in out:
            out.append(glob)
    return out


def settings_json(*, write_roots: list[str], deny_write: list[str] | None = None) -> dict:
    """The settings object: the sandbox on, writes confined to ``write_roots``.

    On a contributor machine ``write_roots`` are :data:`WORKTREE_PLACEHOLDER`, the call's
    own task worktree, and the entrypoint repository's ``.git`` — the second is not
    optional, because a worker commits its document and git writes a linked worktree's
    index, refs and objects inside the repository the worktree was created from, so a
    worktree-only ``allowWrite`` would stop ``git commit``. It does mean a call can write
    another worktree's *git metadata*, though not that worktree's files.

    ``deny_write`` are the paths denied inside them: the out-of-tree gate directory and
    the run's root. Both are outside every ``allowWrite`` root already, so this is belt
    and braces — and belt and braces is what a sandbox nobody here can test deserves.
    """
    return {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "filesystem": {
                "disabled": False,
                "allowWrite": globs(list(write_roots)),
                "denyWrite": globs(list(deny_write or [])),
            },
        },
    }


def write_settings(path: Path, *, write_roots: list[str],
                   deny_write: list[str] | None = None) -> Path:
    """Write :func:`settings_json` to ``path`` (sorted keys, one newline)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings_json(write_roots=write_roots, deny_write=deny_write),
                               indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def staged_settings(root: Path, entrypoint: Path, gate_dir: Path) -> Path:
    """The template a staged run writes: the placeholder, and the entrypoint's ``.git``."""
    return write_settings(Path(root) / SETTINGS_NAME,
                          write_roots=[WORKTREE_PLACEHOLDER, str(Path(entrypoint) / ".git")],
                          deny_write=[str(gate_dir), str(root)])


def call_settings(template: str, worktree: str) -> str:
    """The template with the placeholder replaced by one call's own worktree.

    ``bridge.sh`` does this with ``sed`` and ``pwd -P``; this is the same substitution,
    for the tests and for anything that needs it in Python.
    """
    return template.replace(WORKTREE_PLACEHOLDER, str(worktree).rstrip("/"))
