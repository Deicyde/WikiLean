#!/usr/bin/env python3
"""The contributor's side of contribute mode: claim a package, and send its results back.

docs/PIPELINE.md section 3 (stage 11). One command on a machine this coordinator cannot
reach over ssh::

    python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \\
        [--package loc-7] [--machine-id <id>] [--stage claim|results] --execute

**It is a dry run until ``--execute``**: without it every step that would push prints
what it would do.

**The claim no two machines can share.** ``claim/<run-id>`` holds a single commit whose
one file names this machine and a random nonce. git answers a push of a commit the
remote already holds with "Everything up-to-date" and exit 0, so a claim branch that is
merely the clone's HEAD double-books every package two contributors race for. The push
carries ``--force-with-lease=refs/heads/claim/<run-id>:`` — an *empty* lease, "only if
the remote has no such ref", which overwrites nothing — and this command then fetches
the ref back and starts only if it is its own commit. The lease is not the mechanism;
the unique commit plus the read-back is.

**What is between the two stages.** ``--stage run`` (:mod:`work`): the package laid out as
a run root, one agent call per task, the staged gate over each task's own documents and the
harvest's reconciliation into a corpus with a receipt. What a contributor still supplies by
hand is the three directories the plan names and the package cannot carry — the sources
refetched at their pinned revisions, the candidates and the declaration index — because
fetching and rendering them is stage 9a, which is **TODO** (docs/PIPELINE.md section 10);
``--stage run`` names any that are missing before it runs anything.

**What travels back**: the gated documents, the run's own records, and — when the
contributor has sealed one — a record set, which is checked here by
``scripts/articles/verify_submission.py`` before anything is pushed, so a defect is seen
on this machine and not first on the coordinator's. Never article text.

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
sys.path.insert(0, str(HERE))

import package as package_mod  # noqa: E402
import results as results_mod  # noqa: E402
import sandbox  # noqa: E402
import work as work_mod  # noqa: E402
import workrepo  # noqa: E402

STAGES = ("claim", "run", "results")
#: What a contributor's workdir records about the package it holds, so a second
#: invocation takes the same one instead of reading its own claim branch as a rival's.
CLAIM_JSON = "claim.json"
CLAIM_SCHEMA = "wikilean.contribute.claim/1"


class RunProblem(ValueError):
    """Something a contributor must fix before this machine runs or sends anything."""


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
        for path in sorted(Path(record_set).rglob("*")):
            if path.is_file():
                target = out / results_mod.RECORD_SET / path.relative_to(Path(record_set))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
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
    log("# next: `--stage run` materialises the package, calls a worker per task, gates")
    log("# each task's documents out of tree and harvests them with a receipt.")
    log("# Before it, fill the three directories the plan names and the package cannot")
    log("# carry: the sources refetched at their pinned revisions, the candidates and")
    log("# the declaration index. `--stage run` names any that are missing.")
    log(f"# The agents run under {sandbox.SETTINGS_NAME}, written per call by bridge.sh.")


def run_stage(*, repo: Path, workdir: Path, package: str, worker_command: str = "",
              execute: bool = False, model: str | None = None,
              max_cost: float | None = None, log=print) -> dict:
    """``--stage run``: the middle, as far as it goes without a model (:mod:`work`).

    A dry run calls no model and writes no document; it still materialises the package,
    says which inputs are missing and runs the gate over whatever is there, so a
    contributor sees the shape of the run before paying for one.
    """
    manifest = package_mod.read(package_mod.package_dir(repo, package))
    log(f"package {package}: {manifest['mode']}, {len(manifest['tasks'])} task(s)")
    return work_mod.run_middle(
        package_dir=package_mod.package_dir(repo, package), workdir=Path(workdir),
        run_id=str(manifest.get("run_id") or package), worker_command=worker_command,
        execute=execute, model=model,
        max_cost=manifest.get("max_cost") if max_cost is None else max_cost, log=log)


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
                        help="run this stage only (default: claim, run, then results)")
    parser.add_argument("--worker-command",
                        help="with --stage run: a local command that stands in for the "
                             "model, run once per task in its own worktree")
    parser.add_argument("--model", default=None, help="with --stage run and --execute")
    parser.add_argument("--documents", type=Path, help="the gated documents to send back")
    parser.add_argument("--host", type=Path, help="the run's own records to send back")
    parser.add_argument("--record-set", type=Path,
                        help="a sealed record set to send back; it is checked against the "
                             "intake contract here before anything is pushed")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="recorded and printed, not enforced: no watchdog runs here")
    parser.add_argument("--execute", action="store_true",
                        help="really push; without it every step that would prints what it "
                             "would do")
    args = parser.parse_args(argv)
    stages = (args.stage,) if args.stage else STAGES
    try:
        repo = workrepo.check_clone(args.repo)
        machine = workrepo.machine_id(args.machine_id)
        package = args.package
        if "claim" in stages:
            package = claim(repo=repo, workdir=args.workdir, machine=machine,
                            package=args.package, take_over=args.take_over,
                            execute=args.execute)
            staged_notice()
        documents = args.documents
        if "run" in stages:
            package = package or held(args.workdir).get("package")
            if not package:
                raise RunProblem("no package: claim one first, or pass --package")
            done = run_stage(repo=repo, workdir=args.workdir, package=package,
                             worker_command=args.worker_command or "",
                             execute=args.execute, model=args.model,
                             max_cost=args.max_cost)
            if documents is None and done["receipt"]["actual"]["count"]:
                documents = Path(args.workdir) / work_mod.CORPUS_DIR / done["receipt"]["run_id"]
        if "results" in stages:
            package = package or held(args.workdir).get("package")
            if not package:
                raise RunProblem("no package: claim one first, or pass --package")
            if args.max_cost is not None:
                print(f"  spend cap: USD {args.max_cost} — recorded, not enforced here")
            send_results(repo=repo, workdir=args.workdir, machine=machine, package=package,
                         documents=documents, host=args.host,
                         record_set=args.record_set, max_cost=args.max_cost,
                         execute=args.execute)
    except (RunProblem, package_mod.PackageProblem, results_mod.ResultProblem,
            work_mod.WorkProblem, workrepo.WorkRepoProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"the run failed: {exc!r}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
