#!/usr/bin/env python3
"""The contributor's side of contribute mode: claim a package, run it, seal it, send it.

docs/PIPELINE.md section 3 (stage 11). One command on a machine this coordinator cannot
reach over ssh::

    python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \\
        [--package loc-7] [--machine-id <id>] [--stage claim|run|seal|results] \\
        [--execute]

**A DRY RUN IS THE DEFAULT, and it is the documented way to start.** Without
``--execute``, nothing is pushed and no model is called. Run previews write local
staging files; results previews write and verify a local bundle. The seal stage always
writes its local sealed set and sends nothing, with or without ``--execute``.
``--execute`` is the only thing in this command that starts a paid run or pushes to
the work repo, and no command
line can start a paid run by omitting a flag.

Two more checks that cost nothing, and are worth running before any paid call:

* ``--stage run`` with no ``--execute`` **stages every task's working directory** and
  then reads the rendered prompts, pulling out every path they name and asking whether
  staging put it there. A prompt naming a file that is not staged is the defect that
  stops a paid run halfway; finding it here is free.
* ``--stage results`` with no ``--execute`` runs ``verify_submission.py`` over a sealed
  set — the same nine checks the submission endpoint runs — and pushes nothing.

**The claim no two machines can share.** ``claim/<run-id>`` holds a single commit whose
one file names this machine and a random nonce. git answers a push of a commit the
remote already holds with "Everything up-to-date" and exit 0, so a claim branch that is
merely the clone's HEAD double-books every package two contributors race for. The push
carries ``--force-with-lease=refs/heads/claim/<run-id>:`` — an *empty* lease, "only if
the remote has no such ref", which overwrites nothing — and this command then fetches
the ref back and starts only if it is its own commit. The lease is not the mechanism;
the unique commit plus the read-back is.

**The middle.** ``--stage run`` (:mod:`work`) lays the package out as a run root,
**stages each task's working directory from this machine's own files** (the coordinator
never sends its staged directories — they hold its copy of the article text), calls the
worker once per task through the sandbox, runs the gate out of tree as the runner's own
validation call, retries once on a refusal, and harvests what passed into a corpus with
a receipt. What a contributor supplies by hand is what the package cannot carry: the
sources refetched at their pinned revisions, the candidates, the declaration index and
the search corpus. ``--stage run`` names any that are missing and runs nothing without
them.

**The seal.** ``--stage seal`` (:mod:`seal`) is stage 8 and stage 6 on this machine: the
harvested documents become outcome rows, the rows become a sealed record set, and the
set is put through the intake contract here. It needs ``--set-id``, ``--campaign``,
``--producer-commit`` and ``--created``, because a seal is a statement about bytes and
about who made them and neither is inferred from a working directory.

**What travels back**: the gated documents, the run's own records, and — when the
contributor has sealed one — a record set, which is checked here by
``scripts/articles/verify_submission.py`` before anything is pushed, so a defect is seen
on this machine and not first on the coordinator's. Findings carry passage quotations;
full source-layer files are excluded.

**The spend cap is recorded, not enforced.** No watchdog runs on a contributor machine,
so a person watches the cost and stops the run.

Exit codes: 0 done, 2 refused (the reason on stderr), 3 the command failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE, PIPELINE / "gate", PIPELINE / "run"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import package as package_mod  # noqa: E402
import results as results_mod  # noqa: E402
import sandbox  # noqa: E402
import seal as seal_mod  # noqa: E402
import task as task_mod  # noqa: E402
import work as work_mod  # noqa: E402
import workdir as workdir_mod  # noqa: E402
import workrepo  # noqa: E402

#: The stages in the order a contributor meets them. ``seal`` is not in the default
#: sequence: a set is sealed once, deliberately, with pins a person supplies.
STAGES = ("claim", "run", "seal", "results")
DEFAULT_STAGES = ("claim", "run", "results")
#: Where ``--stage seal`` writes, under the working directory, unless told otherwise.
SET_DIR = "set"
#: What a contributor's workdir records about the package it holds, so a second
#: invocation takes the same one instead of reading its own claim branch as a rival's.
CLAIM_JSON = "claim.json"
CLAIM_SCHEMA = "wikilean.contribute.claim/1"


class RunProblem(ValueError):
    """Something a contributor must fix before this machine runs or sends anything."""


#: What exit 2 means: something a contributor can read and fix. Every one of these is
#: a stage of the pipeline saying no by name, and ``--stage seal`` reaches four that
#: nothing else here does — the tally, the corpus's own gate check, the export and the
#: index reader. They are all ``ValueError`` subclasses, so leaving them out would land
#: them in the ``except (OSError, ValueError, KeyError)`` catch-all and report them as
#: "the run failed" with exit 3, which is for a command that broke and not for a
#: contributor who has something to go and fix.
REFUSALS = (RunProblem, package_mod.PackageProblem, results_mod.ResultProblem,
            seal_mod.SealProblem, work_mod.WorkProblem, workrepo.WorkRepoProblem,
            workdir_mod.WorkdirProblem, task_mod.TaskProblem,
            seal_mod.tally_mod.TallyProblem, seal_mod.corpus_mod.CorpusProblem,
            seal_mod.export_records.ExportProblem, seal_mod.index_mod.IndexProblem)


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def held(workdir: Path) -> dict:
    path = Path(workdir) / CLAIM_JSON
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except ValueError:
        return {}


def claim_payload(package: str, machine: str) -> dict:
    """What the claim commit says: this machine, this package, and a nonce.

    **The nonce is the mechanism.** A commit over this payload is one no other machine
    can produce, which is what makes a second push a real rejection. Nothing of this
    machine's filesystem is in it.
    """
    return {"schema": CLAIM_SCHEMA, "package": package, "machine_id": machine,
            "nonce": secrets.token_hex(16), "at": now()}


def record_claim(workdir: Path, package: str, machine: str, repo: Path,
                 claim: dict | None = None, commit: str = "") -> Path:
    """What this workdir holds, written only once the claim is made."""
    Path(workdir).mkdir(parents=True, exist_ok=True)
    record = {**(claim or {}), "package": package, "machine_id": machine,
              "repo": str(repo), "at": (claim or {}).get("at") or now()}
    if commit:
        record["claim_commit"] = commit
    path = Path(workdir) / CLAIM_JSON
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def claim(*, repo: Path, workdir: Path, machine: str, package: str | None = None,
          take_over: bool = False, execute: bool = False, log=print) -> str:
    """Claim a package, or raise. Returns the package's run id."""
    workdir = Path(workdir)
    mine = held(workdir)
    if mine.get("package") and package in (None, mine["package"]):
        log(f"package {mine['package']}: held by this workdir since {mine.get('at')}")
        return str(mine["package"])
    if workrepo.has_remote(repo) and execute:
        workrepo.fetch(repo, "+refs/heads/*:refs/remotes/origin/*")
        branch = workrepo.current_branch(repo)
        if branch and branch != "HEAD" and workrepo.is_clean(repo):
            # fast-forward only: nothing here may rewrite or merge what the coordinator pushed
            workrepo.git(repo, "merge", "--ff-only", "-q", f"origin/{branch}", check=False)
    claimed = {name.split("/", 1)[1]
               for name in workrepo.branches(repo, f"{workrepo.CLAIM_PREFIX}/*") if "/" in name}
    available = package_mod.available(repo)
    if not available:
        raise RunProblem(f"{repo} holds no package ({workrepo.PACKAGES}/); the coordinator "
                         "publishes one with publish_package.py")
    if package:
        if package not in available:
            raise RunProblem(f"no package {package!r} in {repo} (have: {', '.join(available[:8])})")
        if package in claimed and not take_over:
            raise RunProblem(
                f"the claim on {package} was refused: {workrepo.CLAIM_PREFIX}/{package} is "
                "already pushed, so another machine holds it. Run another package, or pass "
                "--take-over when the machine that claimed it will not finish")
    else:
        free = [name for name in available if name not in claimed]
        if not free:
            raise RunProblem(f"every package in {repo} is claimed already "
                             f"({', '.join(sorted(claimed)[:8])}); ask the coordinator to "
                             "publish another, or name one with --package --take-over")
        package = free[0]
    manifest = package_mod.read(package_mod.package_dir(repo, package))
    branch = f"{workrepo.CLAIM_PREFIX}/{package}"
    log(f"package {package}: {manifest['mode']}, {len(manifest['tasks'])} task(s), "
        f"{len(manifest['sources'])} source(s), cap USD {manifest.get('max_cost')}")
    if not execute:
        # a dry run holds nothing: writing the claim record here would make the next
        # invocation skip the claim it never made
        log(f"# dry run: would claim it by pushing {branch}")
        return package
    payload, commit = claim_payload(package, machine), ""
    if workrepo.has_remote(repo):
        taken = workrepo.remote_sha(repo, branch) if take_over else ""
        commit = workrepo.claim_commit(
            repo, branch, json.dumps(payload, indent=1, sort_keys=True) + "\n",
            f"contribute: {machine} claims {package}", parent=taken)
        pushed = workrepo.push_claim(repo, branch, expect_absent=not taken)
        if pushed.returncode != 0:
            raise RunProblem(
                f"the claim on {package} was refused: {branch} is already pushed, so another "
                f"machine holds it. The push's own words: {(pushed.stderr or '').strip()[:200]}")
        # the exit status is not the answer: git answers a push of a commit the remote
        # already holds with "Everything up-to-date" and exit 0, so the claim holds only
        # if the ref the remote now has is this machine's own commit
        on_remote = workrepo.remote_sha(repo, branch, fetch_first=True)
        if on_remote != commit:
            raise RunProblem(
                f"the claim on {package} is not this machine's: {branch} is "
                f"{(on_remote or 'absent')[:12]} and this machine pushed {commit[:12]}, so "
                "another machine holds it")
        log(f"  claimed  : {branch} ({commit[:12]}, {workrepo.CLAIM_FILE} names {machine} "
            f"and nonce {payload['nonce'][:8]})")
    else:
        log(f"  warning  : {repo} has no remote, so the claim is local only and nothing "
            f"stops a second machine taking {package}")
    record_claim(workdir, package, machine, repo, payload, commit)
    return package


def send_results(*, repo: Path, workdir: Path, machine: str, package: str,
                 documents: Path | None = None, host: Path | None = None,
                 record_set: Path | None = None, max_cost: float | None = None,
                 received_at: str | None = None, execute: bool = False,
                 log=print) -> dict:
    """Lay this machine's result out in the work repo, check it, and push it."""
    manifest = package_mod.read(package_mod.package_dir(repo, package))
    out = Path(repo) / workrepo.RESULTS / package / machine
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    landed = results_mod.copy_tree(Path(documents), out / results_mod.DOCUMENTS, "*.json") \
        if documents else []
    if host:
        results_mod.copy_tree(Path(host), out / results_mod.HOST)
    submission = None
    if record_set:
        results_mod.copy_tree(Path(record_set), out / results_mod.RECORD_SET)
        # the intake contract, run here so a defect is seen on this machine first
        submission = results_mod.verify_record_set(
            out / results_mod.RECORD_SET, machine_id=machine,
            received_at=received_at or now())
        log(f"  set      : accepted by {submission['verifier']} — "
            f"{submission['records']} record(s), seal {submission['seal_sha256'][:12]}")
    tasks = [str(t) for t in manifest.get("tasks") or []]
    merged = sorted({path.parent.name for path in (out / results_mod.DOCUMENTS).rglob("*.json")})
    owed = [task for task in tasks if task not in merged]
    if owed:
        log(f"  warning  : {len(owed)} of {len(tasks)} task(s) have no document "
            f"({', '.join(owed[:5])}); the result is partial")
    receipt_path = results_mod.write_receipt(out, {
        "package": package, "run_id": manifest["run_id"], "machine_id": machine,
        "mode": manifest["mode"], "documents": len(landed),
        "max_cost": max_cost if max_cost is not None else manifest.get("max_cost"),
        "tasks": {"merged": sorted(merged), "owed": owed},
        "producer_commit": manifest.get("producer_commit"),
        "submission": submission,
    })
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    rel = f"{workrepo.RESULTS}/{package}/{machine}"
    branch = f"{workrepo.RESULT_PREFIX}/{package}/{machine}"
    log(f"  results  : {len(landed)} document(s) and the run's own records -> {out} "
        f"({receipt_path.name})")
    # The result is checked where it stands, before the commit moves the clone off it.
    problems = results_mod.check_receipt(out)
    if problems:
        raise results_mod.ResultProblem("this result does not match the receipt just "
                                        "written: " + "; ".join(problems[:5]))
    if not execute:
        log(f"# dry run: would commit {rel} and push {branch}")
        return {"path": str(out), "branch": branch, "documents": len(landed),
                "submission": submission, "receipt": receipt}
    # the clone goes back to the branch it was on, whatever happens: the packages live
    # there, and a clone left on result/… would have the next claim built over this
    # machine's results instead
    was = workrepo.current_branch(repo)
    workrepo.git(repo, "checkout", "-q", "-B", branch)
    try:
        head = workrepo.commit_paths(
            repo, [rel],
            f"contribute: {package} results from {machine} ({len(landed)} document(s))")
        if workrepo.has_remote(repo):
            pushed = workrepo.push(repo, f"{branch}:{branch}")
            if pushed.returncode != 0:
                raise RunProblem(f"pushing {branch} failed: {(pushed.stderr or '').strip()[:300]}"
                                 f"; the commit {head[:12]} is in the clone, on {branch}")
    finally:
        if was and was != "HEAD":
            workrepo.git(repo, "checkout", "-q", was, check=False)
    log(f"  pushed   : {branch} ({head[:12]}); the clone is back on {was}. The result is "
        f"on that branch, not in the working tree the clone is on now.")
    return {"path": str(out), "branch": branch, "documents": len(landed),
            "submission": submission, "receipt": receipt, "commit": head}


def staged_notice(log=print) -> None:
    log("# next: `--stage run` materialises the package, stages each task's working")
    log("# directory from YOUR files, calls a worker per task, runs the gate out of")
    log("# tree and harvests what passed with a receipt.")
    log("# Before it, fill what the plan names and the package cannot carry: the")
    log("# sources refetched at their pinned revisions, the candidates, the")
    log("# declaration index and the search corpus. `--stage run` names any missing.")
    log("# Run it WITHOUT --execute first: it stages everything and checks every path")
    log("# the prompts name, free, and calls no model.")
    log(f"# The agents run under {sandbox.SETTINGS_NAME}, written per call by bridge.sh.")


def run_stage(*, repo: Path, workdir: Path, package: str, worker_command: str = "",
              execute: bool = False, model: str | None = None,
              max_cost: float | None = None, reviewer: bool = True,
              max_attempts: int | None = None, log=print) -> dict:
    """``--stage run``: the middle (:mod:`work`). A dry run unless ``--execute``.

    A dry run materialises the package, stages every task's working directory, says
    which inputs are missing, checks that every path the prompts name is staged, and
    calls no model and writes no document — so a contributor sees the shape of the run,
    and its defects, before paying for one.
    """
    manifest = package_mod.read(package_mod.package_dir(repo, package))
    log(f"package {package}: {manifest['mode']}, {len(manifest['tasks'])} task(s)")
    extra = {} if max_attempts is None else {"max_attempts": max_attempts}
    return work_mod.run_middle(
        package_dir=package_mod.package_dir(repo, package), workdir=Path(workdir),
        run_id=str(manifest.get("run_id") or package), worker_command=worker_command,
        execute=execute, model=model, reviewer=reviewer,
        max_cost=manifest.get("max_cost") if max_cost is None else max_cost, log=log,
        **extra)


def seal_stage(*, repo: Path, workdir: Path, package: str, set_id: str, campaign: str,
               producer_commit: str, created: str, index: Path | None = None,
               out: Path | None = None, sourceset: str = "",
               challenge_runs: tuple[str, ...] = (), runs: Path | None = None,
               byte_budget: int = 0, machine: str = "", received_at: str = "",
               log=print) -> dict:
    """``--stage seal``: tally, export and check a set on this machine (:mod:`seal`).

    The set is written and then put straight through the intake contract, so the
    contributor's last step before sending is the coordinator's first step on
    receiving — the same function, the same refusal codes and the same HTTP statuses.
    Nothing is pushed here.
    """
    package_dir = package_mod.package_dir(repo, package)
    manifest = package_mod.read(package_dir)
    root = Path(workdir) / work_mod.ROOT_DIR
    if not (root / "run.json").is_file():
        raise RunProblem(f"{root} is not a materialised package; run `--stage run` "
                         "first (a dry run is enough to lay the root out)")
    if index is None:
        config = work_mod.gate_config(root)
        named = (config.get("index") or {}).get("path")
        if not named:
            raise RunProblem("this run's plan names no declaration index, so --index "
                             "has to say where yours is: the tally joins every "
                             "location's proof status from it")
        path = Path(str(named))
        index = path if path.is_absolute() else (root / "gate" / path).resolve()
    out = Path(out) if out else Path(workdir) / SET_DIR
    log(f"package {package}: sealing {manifest.get('run_id')} into {out}")
    done = seal_mod.seal(root=root, workdir=Path(workdir), package_dir=package_dir,
                         manifest=manifest, out=out, set_id=set_id, campaign=campaign,
                         producer_commit=producer_commit, created=created, index=index,
                         sourceset=sourceset, challenge_runs=challenge_runs,
                         runs=runs, byte_budget=byte_budget, log=log)
    # the intake contract, here, before anything is pushed: the same answer the
    # coordinator and the endpoint will give, with the rule's own name on a refusal
    submission = results_mod.verify_record_set(
        out, machine_id=machine or "contributor", received_at=received_at or now())
    log(f"  intake   : accepted by {submission['verifier']} — "
        f"{submission['records']} record(s), seal {submission['seal_sha256'][:12]}")
    log(f"# next: --stage results --record-set {out}")
    return {**done, "submission": submission}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path, help="a clone of the work repo")
    parser.add_argument("--workdir", required=True, type=Path,
                        help="where this machine's claim and working files go (created)")
    parser.add_argument("--package", default=None,
                        help="the package to run (default: the first unclaimed one)")
    parser.add_argument("--take-over", action="store_true",
                        help="run a package another machine has claimed, for a machine that "
                             "will not finish. Two machines running one package is two "
                             "results, which collect_results.py takes as two")
    parser.add_argument("--machine-id", default=None,
                        help="this machine's id in results/<package>/<machine-id>/ "
                             "(default: $WIKILEAN_MACHINE_ID, else the host name)")
    parser.add_argument("--stage", choices=STAGES, default=None,
                        help="run this stage only (default: claim, run, then results; "
                             "seal is never in the default sequence)")
    parser.add_argument("--worker-command",
                        help="with --stage run: a local command that stands in for the "
                             "model, run once per task in its own worktree")
    parser.add_argument("--model", default=None, help="with --stage run and --execute")
    parser.add_argument("--no-reviewer", action="store_true",
                        help="with --stage run: skip the form-only reviewer call, which "
                             "is one model call per task of your own money")
    parser.add_argument("--max-attempts", type=int, default=None,
                        help="with --stage run: worker calls per task; 2 (the default) "
                             "is the first call and one retry on a gate refusal")
    parser.add_argument("--set-id", default=None, help="with --stage seal: the set's id")
    parser.add_argument("--campaign", default=None,
                        help="with --stage seal: the campaign token the set is sealed "
                             "under, 'run:<run>[+<challenge run>]'")
    parser.add_argument("--producer-commit", default=None,
                        help="with --stage seal: the 40-hex commit of the WikiLean "
                             "checkout that produced it (git rev-parse HEAD)")
    parser.add_argument("--created", default=None,
                        help="with --stage seal: the set's creation time, ISO 8601 UTC "
                             "ending in Z; given so the set is a function of its inputs")
    parser.add_argument("--sourceset", default="",
                        help="with --stage seal: the sourceset's id, when the plan "
                             "records none")
    parser.add_argument("--index", type=Path, default=None,
                        help="with --stage seal: the declaration index the tally joins "
                             "proof statuses from (default: the one the plan names)")
    parser.add_argument("--challenge-run", action="append", default=[],
                        help="with --stage seal: a challenge run whose verdicts apply. "
                             "Nothing leaves this pipeline without a challenge value, "
                             "so a set with no challenge run exports no record")
    parser.add_argument("--runs", type=Path, default=None,
                        help="with --stage seal: where this machine's own runs are — "
                             "its cut and its challenge runs "
                             f"(default: <workdir>/{seal_mod.RUNS_DIR})")
    parser.add_argument("--byte-budget", type=int, default=0,
                        help="with --stage seal: a per-source byte budget (0: none)")
    parser.add_argument("--set-out", type=Path, default=None,
                        help="with --stage seal: where the set is written "
                             f"(default: <workdir>/{SET_DIR})")
    parser.add_argument("--documents", type=Path, help="the gated documents to send back")
    parser.add_argument("--host", type=Path, help="the run's own records to send back")
    parser.add_argument("--record-set", type=Path,
                        help="a sealed record set to send back; it is checked against the "
                             "intake contract here before anything is pushed")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="recorded and printed, not enforced: no watchdog runs here")
    parser.add_argument("--execute", action="store_true",
                        help="really claim, really call a model, really push; without it "
                             "every step that would prints what it would do")
    args = parser.parse_args(argv)
    stages = (args.stage,) if args.stage else DEFAULT_STAGES
    try:
        repo = workrepo.check_clone(args.repo)
        machine = workrepo.machine_id(args.machine_id)
        package = args.package
        # `seal` is not one of the stages --execute governs: it writes a set and pushes
        # nothing either way. Preview stages can also write local staging/result files;
        # describe the model and push boundary explicitly.
        if not args.execute and set(stages) - {"seal"}:
            print("# DRY RUN: nothing is pushed and no model is called; local staging "
                  "and result files may be written.\n# This is the default. Read what follows, then add "
                  "--execute if you agree with it.")
        if "seal" in stages:
            print("# --stage seal writes a record set and pushes nothing, with or "
                  "without --execute.\n# `--stage results` is what sends it.")
        if "claim" in stages:
            package = claim(repo=repo, workdir=args.workdir, machine=machine,
                            package=args.package, take_over=args.take_over,
                            execute=args.execute)
            staged_notice()
        documents = args.documents
        record_set = args.record_set
        if "run" in stages:
            package = package or held(args.workdir).get("package")
            if not package:
                raise RunProblem("no package: claim one first, or pass --package")
            done = run_stage(repo=repo, workdir=args.workdir, package=package,
                             worker_command=args.worker_command or "",
                             execute=args.execute, model=args.model,
                             reviewer=not args.no_reviewer,
                             max_attempts=args.max_attempts,
                             max_cost=args.max_cost)
            if documents is None and done["receipt"]["actual"]["count"]:
                documents = Path(args.workdir) / work_mod.CORPUS_DIR / done["receipt"]["run_id"]
        if "seal" in stages:
            package = package or held(args.workdir).get("package")
            if not package:
                raise RunProblem("no package: claim one first, or pass --package")
            missing = [name for name, value in
                       (("--set-id", args.set_id), ("--campaign", args.campaign),
                        ("--producer-commit", args.producer_commit),
                        ("--created", args.created)) if not value]
            if missing:
                raise RunProblem(
                    f"--stage seal needs {', '.join(missing)}: a seal pins the set's "
                    "id, its campaign, the commit of the code that produced it and "
                    "when it was made, and none of those is a thing to infer from a "
                    "working directory")
            sealed = seal_stage(
                repo=repo, workdir=args.workdir, package=package, set_id=args.set_id,
                campaign=args.campaign, producer_commit=args.producer_commit,
                created=args.created, index=args.index, out=args.set_out,
                sourceset=args.sourceset,
                challenge_runs=tuple(args.challenge_run), runs=args.runs,
                byte_budget=args.byte_budget, machine=machine)
            if record_set is None:
                record_set = Path(sealed["set_dir"])
        if "results" in stages:
            package = package or held(args.workdir).get("package")
            if not package:
                raise RunProblem("no package: claim one first, or pass --package")
            if args.max_cost is not None:
                print(f"  spend cap: USD {args.max_cost} — recorded, not enforced here")
            send_results(repo=repo, workdir=args.workdir, machine=machine, package=package,
                         documents=documents, host=args.host,
                         record_set=record_set, max_cost=args.max_cost,
                         execute=args.execute)
    except REFUSALS as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"the run failed: {exc!r}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
