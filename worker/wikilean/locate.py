"""The locate unit: the whole contributor pipeline on one article, then intake.

This unit is **off unless the contributor turns it on twice** — ``locate_enabled``
in the config file and ``--enable-locate`` on the command line — because
docs/START-CONTRIBUTING.md path 3 says to open an issue before running it, and
its prerequisites are not all open yet. Nothing here is a reimplementation: the
unit drives the scripts the repository already ships,

    scripts/pipeline/contribute/run_package.py   claim → run → seal
    scripts/articles/submit.py                   the sealed set to /api/submissions

from a WikiLean checkout the contributor names. Two consequences are worth
stating plainly:

* The paid model stages belong to ``run_package.py --stage run --execute``, and
  no watchdog runs on a contributor's machine (START-CONTRIBUTING, "What it
  costs you"). Turning both switches on is the consent for that.
* ``submit.py`` reads ``WIKILEAN_AGENT_TOKEN`` from its environment. This is the
  one child process that is given the token, it is WikiLean's own code, it
  calls no model, and the token is passed in the environment rather than on
  the command line. The model child (model.py) never sees it.

:func:`preconditions` is the honest part of the unit: it names everything that
has to be in place, and ``wikilean doctor`` prints it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import journal
from .config import Config
from .secrets import ENV_TOKEN, load_token

RUN_PACKAGE = "scripts/pipeline/contribute/run_package.py"
SUBMIT = "scripts/articles/submit.py"


@dataclass(frozen=True)
class Missing:
    what: str
    detail: str


def preconditions(config: Config) -> list[Missing]:
    """Everything that must be true before the unit can run, and is not."""
    missing: list[Missing] = []
    if not config.locate_enabled:
        missing.append(Missing("the config flag", 'set "locate_enabled": true in config.json'))
    checkout = Path(config.locate_checkout).expanduser() if config.locate_checkout else None
    if not checkout or not checkout.is_dir():
        missing.append(Missing("a WikiLean checkout", 'set "locate_checkout" to a clone of Deicyde/WikiLean'))
    else:
        for script in (RUN_PACKAGE, SUBMIT):
            if not (checkout / script).is_file():
                missing.append(Missing(f"{script}", f"not in {checkout}"))
    if not config.locate_repo:
        missing.append(Missing("a work repository", 'set "locate_repo" to the clone you were given'))
    elif not Path(config.locate_repo).expanduser().is_dir():
        missing.append(Missing("the work repository", f"{config.locate_repo} is not a directory"))
    if not config.locate_workdir:
        missing.append(Missing("a work directory", 'set "locate_workdir" to a scratch directory'))
    if not shutil.which("python3"):
        missing.append(Missing("python3", "the pipeline runs under CPython 3.12"))
    if load_token() is None:
        missing.append(Missing("an agent token", "run `wikilean login`; the intake upload needs it"))
    return missing


def steps(config: Config) -> list[tuple[str, list[str]]]:
    """The exact commands, in order, that one locate unit runs.

    Kept pure so ``--dry-run`` prints precisely what a real run would execute,
    and so a test can read the sequence without a checkout.

    **Known gap.** ``--stage seal`` also takes ``--set-id``, ``--campaign``,
    ``--producer-commit``, ``--created`` and ``--challenge-run``
    (docs/START-CONTRIBUTING.md step 5). Those identify the claimed package
    and the challenge run that judged it, and this CLI does not know them
    until a package has been claimed — the claim writes them into the work
    directory. Reading them back out of a claimed package is the piece of this
    unit that is wired but not finished; worker/README.md says so too.
    """
    checkout = str(Path(config.locate_checkout).expanduser())
    repo = str(Path(config.locate_repo).expanduser())
    workdir = str(Path(config.locate_workdir).expanduser())
    package = [f"{checkout}/{RUN_PACKAGE}", "--repo", repo, "--workdir", workdir]
    return [
        ("claim a package (preview)", ["python3", *package, "--stage", "claim"]),
        ("claim a package", ["python3", *package, "--stage", "claim", "--execute"]),
        ("stage and check every prompt (free, calls no model)", ["python3", *package, "--stage", "run"]),
        ("the paid model run", ["python3", *package, "--stage", "run", "--execute"]),
        ("seal the result locally and check it offline", ["python3", *package, "--stage", "seal"]),
        ("submit the sealed set (preview)", ["python3", f"{checkout}/{SUBMIT}", "--record-set", f"{workdir}/set"]),
        ("submit the sealed set", ["python3", f"{checkout}/{SUBMIT}", "--record-set", f"{workdir}/set",
                                   "--execute", "--wait", "60"]),
    ]


def child_environment(step_name: str, token: str | None) -> dict[str, str]:
    """The environment one step runs in.

    Only the submission steps are given the agent token, and only in the
    environment. The pipeline's own model stages run under the contributor's
    Claude exactly as the repository's scripts invoke it; this CLI does not
    reach into that.
    """
    environment = dict(os.environ)
    environment.pop(ENV_TOKEN, None)
    if token and "submit" in step_name:
        environment[ENV_TOKEN] = token
    return environment


def run_locate(config: Config, options, *, log=print):
    """Drive the unit, or explain exactly why it will not run."""
    from .work import EX_FAILED, EX_NOTHING, EX_OK, Outcome

    if not (config.locate_enabled and options.enable_locate):
        return Outcome(EX_NOTHING, unit="locate",
                       summary="locate is off: it needs --enable-locate and \"locate_enabled\": true, "
                               "and docs/START-CONTRIBUTING.md asks you to open an issue first")
    gaps = preconditions(config)
    if gaps:
        for gap in gaps:
            log(f"  missing {gap.what}: {gap.detail}")
        journal.write("round", ok=False, unit="locate", stage="preconditions",
                      missing=[gap.what for gap in gaps])
        return Outcome(EX_FAILED, unit="locate", summary=f"{len(gaps)} prerequisite(s) missing")

    plan = steps(config)
    if options.dry_run:
        log("\n--- would run --------------------------------------------------")
        for name, argv in plan:
            log(f"# {name}\n" + " ".join(argv))
        log("--- and nothing else -------------------------------------------\n")
        journal.write("round", ok=True, unit="locate", dry_run=True, sent=False,
                      steps=[name for name, _ in plan])
        return Outcome(EX_OK, unit="locate", summary=f"dry run: {len(plan)} steps")

    stored = load_token()
    token = stored.token if stored else None
    for name, argv in plan:
        log(f"  {name}")
        journal.write("locate-step", step=name, argv=argv)
        done = subprocess.run(argv, env=child_environment(name, token),
                              cwd=str(Path(config.locate_checkout).expanduser()))
        if done.returncode != 0:
            journal.write("round", ok=False, unit="locate", stage=name, exit=done.returncode)
            return Outcome(EX_FAILED, unit="locate", summary=f"{name} exited {done.returncode}")
    journal.write("round", ok=True, unit="locate", sent=True, result="submitted")
    return Outcome(EX_OK, unit="locate", summary="a sealed set was submitted", wrote=True)
