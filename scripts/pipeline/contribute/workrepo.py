#!/usr/bin/env python3
"""The work repo: the git transport contribute mode uses instead of ssh.

docs/PIPELINE.md section 3 (stage 11), and `README.md` beside this file. One private
repository, cloned on the coordinator's machine and on every contributor's, carries
packages one way and results the other::

    packages/<run-id>/                 a planned locate run (the coordinator writes it)
    results/<run-id>/<machine-id>/     what a contributor sends back

Branches: the clone's own branch holds the packages, ``claim/<run-id>`` is a
contributor's claim on one, and ``result/<run-id>/<machine-id>`` is what it sends back.

Nothing here force pushes, rewrites history or touches a branch it did not create, and
nothing here reads or writes a credential: the clone's own remote and the operator's git
configuration do the authentication. The one ``--force-with-lease`` is
:func:`push_claim`'s, whose lease is *empty* — "push only if the remote has no such ref"
— so it can overwrite nothing; it is a refusal, not a force.

This module runs git and nothing else. What a package or a result *is* belongs to
``package.py`` and ``results.py``.

**Never ``git add -A``** (AGENTS.md, the bot pipeline's rule, and the same reason here):
a work repo is edited by a coordinator and several contributors, and a stray file in one
clone is not this commit's business. :func:`commit_paths` stages the paths it is given.
"""
from __future__ import annotations

import io
import os
import platform
import re
import subprocess
import tarfile
from pathlib import Path

#: Where packages and results live in the work repo.
PACKAGES = "packages"
RESULTS = "results"
#: The branch a contributor creates to claim a package, and the one it pushes back.
CLAIM_PREFIX = "claim"
RESULT_PREFIX = "result"
#: The one file a claim commit carries: who holds the package, with the nonce that makes
#: this machine's claim commit its own (:func:`push_claim`).
CLAIM_FILE = "claim.json"
#: A machine id: a path component and a branch component, so no "/" and no leading ".".
MACHINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: The ``-c`` pairs every call carries: the settings that name a program git would
#: otherwise run on the repository's behalf. ``core.sshCommand`` and ``core.askPass`` are
#: deliberately left out — a push must still reach the remote the operator cloned from,
#: and its credential helper is theirs.
SAFE_CONFIG: tuple[str, ...] = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
)


class WorkRepoProblem(RuntimeError):
    """Something about the work repo or a git call this transport refuses to work around."""


def git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None,
        stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run git in ``repo``; ``check`` raises on a non-zero exit.

    The one place this transport builds a git argv. ``env`` adds to this process's
    environment, and ``stdin`` is what the plumbing of :func:`claim_commit` reads.
    """
    argv = ["git", "-C", str(repo), *SAFE_CONFIG, *args]
    done = subprocess.run(argv, text=True, capture_output=True, input=stdin,
                          env=None if env is None else {**os.environ, **env})
    if check and done.returncode != 0:
        raise WorkRepoProblem(f"git {' '.join(args)} failed in {repo} ({done.returncode}): "
                              f"{(done.stderr or done.stdout).strip()[:400]}")
    return done


def check_clone(repo: str | Path) -> Path:
    """The work repo clone, or a refusal naming what is wrong with it."""
    path = Path(repo).expanduser()
    if not path.is_dir():
        raise WorkRepoProblem(f"--repo {path} is not a directory; clone the work repo first")
    done = git(path, "rev-parse", "--show-toplevel", check=False)
    if done.returncode != 0:
        raise WorkRepoProblem(f"--repo {path} is not a git checkout: the work repo is a clone "
                              "of one private repository")
    return Path(done.stdout.strip() or path)


def machine_id(given: str | None = None) -> str:
    """This machine's id: ``given``, ``$WIKILEAN_MACHINE_ID``, else the host name.

    Lower-cased, with anything outside the grammar turned into ``-``. A name that is
    empty after that is refused rather than guessed at, since the result's path and its
    branch are keyed on it.
    """
    raw = str(given or os.environ.get("WIKILEAN_MACHINE_ID") or platform.node() or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.").lower()[:64]
    if not slug or not MACHINE_ID.match(slug):
        raise WorkRepoProblem(
            f"no usable machine id (from {raw!r}); pass --machine-id, or set "
            "WIKILEAN_MACHINE_ID: it names this machine's results in the work repo")
    return slug


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def is_clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain").stdout.strip()


def has_remote(repo: Path, remote: str = "origin") -> bool:
    return remote in git(repo, "remote").stdout.split()


def commit_paths(repo: Path, paths: list[str], message: str) -> str:
    """Add exactly ``paths`` and commit; returns the new HEAD. Never ``git add -A``."""
    git(repo, "add", "--", *paths)
    if not git(repo, "diff", "--cached", "--name-only").stdout.strip():
        raise WorkRepoProblem("nothing to commit: the work repo already holds exactly these "
                              "bytes (a re-publish of an unchanged package is a no-op)")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def fetch(repo: Path, *refspecs: str, remote: str = "origin") -> None:
    git(repo, "fetch", "--quiet", remote, *refspecs)


def push(repo: Path, refspec: str, *, remote: str = "origin") -> subprocess.CompletedProcess:
    """Push one refspec; the caller reads ``returncode`` (a claim race is not an error here)."""
    return git(repo, "push", remote, refspec, check=False)


def claim_commit(repo: Path, branch: str, payload: str, message: str, *,
                 parent: str = "") -> str:
    """Point ``branch`` at a new commit carrying ``payload`` as :data:`CLAIM_FILE`.

    Plumbing, not a checkout — ``hash-object``, ``mktree``, ``commit-tree``,
    ``update-ref`` — so the clone's HEAD and working tree never move: the packages live
    on the branch the clone is on, and a claim must not disturb them. The commit has no
    parent unless ``parent`` names one, which only a take-over does: it commits **on top
    of** the claim it takes over, so that push is an ordinary fast-forward and rewrites
    nothing.
    """
    blob = git(repo, "hash-object", "-w", "--stdin", stdin=payload).stdout.strip()
    tree = git(repo, "mktree", stdin=f"100644 blob {blob}\t{CLAIM_FILE}\n").stdout.strip()
    argv = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    commit = git(repo, *argv).stdout.strip()
    git(repo, "update-ref", f"refs/heads/{branch}", commit)
    return commit


def push_claim(repo: Path, branch: str, *, expect_absent: bool,
               remote: str = "origin") -> subprocess.CompletedProcess:
    """Push the claim branch; the caller reads ``returncode`` **and** the ref afterwards.

    With ``expect_absent`` the refspec carries ``--force-with-lease=<ref>:`` — an *empty*
    expected object name, which git-push(1) reads as "the ref must not already exist". It
    can overwrite nothing, so it is the opposite of a force push: a second machine whose
    claim commit differs is rejected with "stale info" instead of creating the branch.

    **The lease alone is not the mechanism.** git answers a push of a commit the remote
    already holds with "Everything up-to-date" and exit 0, lease or no lease, so two
    contributors who had both just fast-forwarded to the same ``origin/main`` and pushed
    HEAD as their claim would both be told they hold the package — the defect this
    replaced. What settles it is that a claim commit is unique to its machine
    (:func:`claim_commit` over a payload naming the machine and a random nonce) and that
    the caller reads the pushed ref back (:func:`remote_sha`) and starts only if it is
    its own commit.
    """
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    lease = [f"--force-with-lease=refs/heads/{branch}:"] if expect_absent else []
    return git(repo, "push", *lease, remote, refspec, check=False)


def remote_sha(repo: Path, branch: str, *, remote: str = "origin",
               fetch_first: bool = False) -> str:
    """The remote's sha for ``branch``, or ``""`` when it has none.

    ``fetch_first`` asks the remote itself rather than this clone's last fetch, which is
    what reading a claim back after a push needs.
    """
    if fetch_first:
        git(repo, "fetch", "--quiet", remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}", check=False)
    done = git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}",
               check=False)
    return done.stdout.strip()


def branches(repo: Path, pattern: str, *, remote: str = "origin") -> list[str]:
    """Remote branch names matching ``pattern``, without the ``<remote>/`` prefix."""
    out = git(repo, "for-each-ref", "--format=%(refname:strip=3)",
              f"refs/remotes/{remote}/{pattern}").stdout
    return sorted(name for name in out.split("\n") if name.strip())


def file_at(repo: Path, ref: str, path: str) -> str:
    """One file's bytes at ``ref`` as text, without moving the clone's HEAD."""
    return git(repo, "show", f"{ref}:{path}").stdout


def safe_member(dest: Path, name: str) -> Path:
    """Where an archive member may land under ``dest``, or a refusal.

    A tar is untrusted input even when git wrote it: a member whose name climbs out of
    the destination is refused rather than written.
    """
    dest = Path(dest)
    target = (dest / name).resolve()
    root = str(dest.resolve())
    if target != Path(root) and not str(target).startswith(root + os.sep):
        raise WorkRepoProblem(f"an archive member leaves the destination ({name})")
    return target


def extract(repo: Path, ref: str, prefix: str, dest: Path) -> list[str]:
    """Extract ``prefix`` from ``ref`` into ``dest``; returns the paths that landed.

    A result is pushed on its own branch, so this reads it out of the fetched ref rather
    than checking it out: the clone's HEAD never moves, and several machines' results are
    collected in one go. Only regular files under ``prefix`` are written, and a member
    that would leave ``dest`` is refused — a tar archive is untrusted input even when git
    wrote it.
    """
    argv = ["git", "-C", str(repo), *SAFE_CONFIG, "archive", "--format=tar", ref, "--", prefix]
    done = subprocess.run(argv, capture_output=True)  # bytes, not text: it is a tar
    if done.returncode != 0:
        raise WorkRepoProblem(f"git archive {ref} -- {prefix} failed ({done.returncode}): "
                              f"{done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    landed: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            target = safe_member(dest, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            target.write_bytes(handle.read() if handle else b"")
            landed.append(member.name)
    return sorted(landed)
