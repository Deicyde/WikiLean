#!/usr/bin/env python3
"""Contribute mode through WikiLean's claims API: claim, fetch, install, renew, link, release.

docs/PIPELINE.md section 3 (stage 11). ``run_package.py --claims`` is contribute mode
for a machine with an agent token and no work repository: the coordinator publishes
each package through the claims API (``publish_package.py --envelope``), and this
module is the contributor's side of it (wiki/src/claims.ts):

* **claim** — ``POST /api/claims`` with a request key written to disk first, so a
  retried claim is the same claim; then the package's envelope, downloaded resumably
  and checked against the sha the claim names, unpacked to ``<workdir>/packages/<run>/``
  (``envelope.unpack``), and its gate code held to the public commit it says it comes
  from (:func:`gate_code_problems`), because that code runs on this machine.
* **index** — the index bundle the package names, into a cache, and its index and
  search corpus linked into the root where the gate and staging read them.
* **renew** — before a paid run and after a task once half an hour has passed, so a long run keeps its lease;
  a claim that ended stops the run after the task in hand.
* **link** — an intake receipt joined to the claim, so the coordinator finds it.
* **release** — the claim ended with a reason, ``done`` once a receipt is linked.

``claim.json`` (schema ``wikilean.contribute.claim/2``) records the claim in the
working directory, and never the token. No network here: every request is
``scripts/articles/contrib_transport.py``'s.
"""
from __future__ import annotations

import datetime as _dt
import importlib.machinery
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
CHECKOUT = PIPELINE.parents[1]
for _path in (str(HERE), str(PIPELINE), str(PIPELINE / "gate"), str(CHECKOUT / "scripts" / "articles")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import envelope as envelope_mod  # noqa: E402
import keys as keys_mod  # noqa: E402 — scripts/pipeline/keys.py
import package as package_mod  # noqa: E402
import stage as stage_mod  # noqa: E402 — scripts/pipeline/gate/stage.py, what a gate holds
import work as work_mod  # noqa: E402
import workdir as workdir_mod  # noqa: E402

CLAIM_SCHEMA = "wikilean.contribute.claim/2"
CLAIM_JSON = "claim.json"
REQUEST_JSON = "claim-request.json"
ENVELOPES_DIR = "envelopes"
SUBMISSIONS_DIR = "submissions"
TOKEN_ENV = "WIKILEAN_AGENT_TOKEN"
RELEASE_REASONS = ("done", "sources_mismatch", "gave_up", "other")
#: The bundle's declarations-and-tags part, which no stage of a run reads.
SKIP_PARTS = ("names/",)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
#: The renew refusals that mean the claim is over (wiki/src/claims.ts ``ownLive``):
#: ended or its package moved, its lease ran out, or this agent holds no such claim.
ENDED_CODES = ("lease", "claim_expired", "not_found")
#: Seconds between a paid run's renewals: the lease is 24 hours, a write costs budget.
RENEW_EVERY = 30 * 60


class ClaimProblem(ValueError):
    """The claims API said no, or this working directory cannot go on as asked."""


class NothingFree(ClaimProblem):
    """No package is free to claim: nothing to do, which is not a failure."""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def transport():
    import contrib_transport  # noqa: PLC0415 — the network is scripts/articles'

    return contrib_transport


def _call(base: str, path: str, token: str, payload: dict | None = None) -> dict:
    """One claims-API request; the service's refusal becomes a ClaimProblem naming its code."""
    client = transport()
    try:
        return client.call(base, path, token, payload)
    except client.Refusal as exc:
        problem = ClaimProblem(f"the claims API refused {path}: {exc}")
        problem.code = exc.code
        raise problem from None


# ---- what this working directory holds -----------------------------------------------


def held(workdir: Path) -> dict | None:
    """The claim this working directory records, or None (git mode's claim.json is not one)."""
    path = Path(workdir) / CLAIM_JSON
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == CLAIM_SCHEMA else None


def live(record: dict | None) -> bool:
    return bool(record) and record.get("state") == "live"


def save(workdir: Path, claim: dict, base: str, **extra) -> dict:
    """Record the API's view of a claim; returns the record written."""
    package = claim.get("package") or {}
    record = {
        "schema": CLAIM_SCHEMA, "base_url": transport().origin(base),
        "claim_id": claim.get("id"), "run_id": claim.get("run_id"),
        "agent_id": claim.get("agent_id"), "state": claim.get("state"),
        "claimed_at": claim.get("claimed_at"), "lease_until": claim.get("lease_until"),
        "expires_at": claim.get("expires_at"), "ended_at": claim.get("ended_at"),
        "end_reason": claim.get("end_reason"), "package_url": claim.get("package_url"),
        "bundle_url": claim.get("bundle_url"),
        "envelope_sha256": package.get("envelope_sha256"), "bytes": package.get("bytes"),
        "bundle_sha256": package.get("bundle_sha256"), "campaign": package.get("campaign"),
        "index_sha256": package.get("index_sha256"),
        "request_key": extra.get("request_key", (held(workdir) or {}).get("request_key")),
        "recorded_at": now(),
    }
    if not (isinstance(record["claim_id"], str) and isinstance(record["run_id"], str)):
        raise ClaimProblem("the claims API answered with no claim id or run id")
    package_mod.check_run_id(record["run_id"])
    Path(workdir).mkdir(parents=True, exist_ok=True)
    path = Path(workdir) / CLAIM_JSON
    scratch = path.with_name(f".{CLAIM_JSON}.part")
    scratch.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(scratch, path)
    return record


def request_key(workdir: Path, package: str | None) -> str:
    """The idempotency key of the next claim, written before it is sent.

    A claim that reached the Worker and whose answer was lost is the same claim when
    the request is sent again with the same key. A key a recorded claim was made with
    is spent: the Worker would answer it with that claim, ended or not.
    """
    path = Path(workdir) / REQUEST_JSON
    spent = (held(workdir) or {}).get("request_key")
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = None
    # a retry that names no package reuses the pending key whatever it named: the Worker
    # answers that key with the claim it made, so a lost answer leaves no orphan claim
    if isinstance(pending, dict) and package in (None, pending.get("run_id")) \
            and isinstance(pending.get("request_key"), str) \
            and re.fullmatch(r"[0-9a-f]{32}", pending["request_key"]) \
            and pending["request_key"] != spent:
        return pending["request_key"]
    key = secrets.token_hex(16)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"request_key": key, "run_id": package, "at": now()},
                               indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return key


def package_dir(workdir: Path, record: dict) -> Path:
    """``<workdir>/packages/<run id>``: claims mode's package, where git mode's clone keeps it."""
    return package_mod.package_dir(Path(workdir), record["run_id"])


# ---- claim --------------------------------------------------------------------------


def claim(*, base: str, token: str, workdir: Path, package: str | None = None,
          execute: bool = False, log=print) -> str:
    """Claim a package, or carry on with the one this working directory holds; its run id.

    A dry run asks only for the package list and claims nothing. NothingFree means no
    package is free to claim.
    """
    workdir = Path(workdir)
    mine = held(workdir)
    if live(mine):
        if package not in (None, mine["run_id"]):
            raise ClaimProblem(f"{workdir} holds claim {mine['claim_id']} on {mine['run_id']}; "
                               "release it (--stage release) or use another --workdir")
        log(f"package {mine['run_id']}: claim {mine['claim_id']} held by this workdir, lease "
            f"until {stamp(mine.get('lease_until'))}")
        if execute:
            mine = renew(base=base, token=token, workdir=workdir, log=log)
            fetch_package(base=base, token=token, workdir=workdir, record=mine,
                          execute=True, log=log)
        return mine["run_id"]
    if not execute:
        listed = _call(base, "/api/packages", token).get("packages") or []
        free = [p for p in listed if p.get("available") and package in (None, p.get("run_id"))]
        if not free:
            raise NothingFree(f"no package is free to claim"
                              + (f" under the name {package}" if package else "")
                              + f" ({len(listed)} open)")
        pick = free[0]
        log(f"package {pick['run_id']}: {pick.get('mode')}, {pick.get('tasks')} task(s), "
            f"{pick.get('sources')} source(s), {pick.get('bytes')} bytes, cap USD "
            f"{pick.get('max_cost')}")
        log("# dry run: would claim it, download its package and check its gate code")
        return str(pick["run_id"])
    for _attempt in range(2):
        key = request_key(workdir, package)
        answer = _call(base, "/api/claims", token,
                       {"request_key": key, **({"run_id": package} if package else {})})
        if not answer.get("claim"):
            raise NothingFree("no package is free to claim"
                              + (f" under the name {package}" if package else ""))
        record = save(workdir, answer["claim"], base, request_key=key)
        if live(record):
            break
        # a retried key answers with the claim it made, which may have ended since; the
        # key is spent now, so the next request is a new claim
        log(f"  ended    : {record['claim_id']} on {record['run_id']} is {record['state']}; "
            "claiming again")
    else:
        raise ClaimProblem(f"the claim on {record['run_id']} ended as it was made "
                           f"({record['state']})")
    log(f"  claimed  : {record['run_id']} as {record['claim_id']}, lease until "
        f"{stamp(record.get('lease_until'))}")
    fetch_package(base=base, token=token, workdir=workdir, record=record, execute=True,
                  log=log)
    return record["run_id"]


def stamp(millis) -> str:
    if not isinstance(millis, (int, float)):
        return "?"
    try:
        moment = _dt.datetime.fromtimestamp(millis / 1000, _dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "?"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_package(*, base: str, token: str, workdir: Path, record: dict,
                  execute: bool = False, log=print) -> tuple[Path, dict]:
    """The claimed package, downloaded and unpacked once, its gate code checked every time.

    Only ``execute`` downloads: a dry run reads a package already unpacked, or refuses.
    """
    workdir = Path(workdir)
    target = package_dir(workdir, record)
    if not (target / package_mod.PACKAGE_JSON).is_file():
        if not execute:
            raise ClaimProblem(f"the package of claim {record.get('claim_id')} is not "
                               f"downloaded yet ({target}); --stage claim --execute "
                               "downloads it")
        sha, size = record.get("envelope_sha256"), record.get("bytes")
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)
                and isinstance(size, int) and 0 < size <= envelope_mod.MAX_ENVELOPE_BYTES):
            raise ClaimProblem("the claim names no envelope sha256 and size to download "
                               f"(at most {envelope_mod.MAX_ENVELOPE_BYTES} bytes)")
        dest = workdir / ENVELOPES_DIR / f"{sha}.json"
        transport().download(base, record["package_url"], token, dest, size=size, sha256=sha)
        target, _manifest = envelope_mod.unpack(dest.read_bytes(), target.parent)
        log(f"  package  : {target} (envelope {sha[:12]})")
    manifest = package_mod.read(target)
    if manifest.get("run_id") != record["run_id"]:
        raise ClaimProblem(f"{target} holds run {manifest.get('run_id')!r}, and the claim is "
                           f"on {record['run_id']!r}")
    problems = gate_code_problems(target, manifest)
    if problems:
        raise ClaimProblem("the package's gate code is not the commit it names, and it would "
                           "run on this machine: " + "; ".join(problems[:5])
                           + ". Release the claim with --stage release --reason other")
    return target, manifest


#: What Python would import as code from a directory on ``sys.path``: source, bytecode,
#: extension modules, and the path and archive files that add more of them. Compared
#: casefolded, because the default macOS disk opens ``VOCAB.PY`` as ``vocab.py``.
IMPORTABLE = tuple(sorted({suffix.casefold() for suffix in importlib.machinery.all_suffixes()}
                          | {".py", ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".pth", ".zip",
                             ".egg", ".whl"}))
GATE_DATA = re.compile(r"gate\.json|tasks/[A-Za-z0-9][A-Za-z0-9._-]*\.json")


def importable(name: str) -> bool:
    return name.casefold().endswith(IMPORTABLE)


def gate_code_problems(directory: Path, manifest: dict, checkout: Path | None = None) -> list[str]:
    """Whether the only code the package carries is the gate of the public commit it names.

    The runner runs the package's own ``gate/verify_locate.py`` and
    ``gate/passage_ids.py`` with this machine's Python (``run/task.py``), and
    ``verify_locate.py`` puts both ``gate/`` and the root above it on ``sys.path``. So
    ``gate/`` holds exactly what ``gate/stage.py`` puts there: every one of its named
    files, each byte for byte the file at ``producer_commit``, a commit on this checkout's
    ``origin/main``, plus ``gate.json`` and ``tasks/<task>.json``, which are the plan's
    data; and nothing anywhere else in the package is a file Python would import.
    """
    checkout = CHECKOUT if checkout is None else checkout
    commit = str(manifest.get("producer_commit") or "")
    if not COMMIT_RE.fullmatch(commit):
        return ["the package names no producer commit"]
    sources = {name: source.relative_to(stage_mod.ROOT).as_posix()
               for source, name in stage_mod.GATE_FILES}
    problems: list[str] = []
    gate: dict[str, str] = {}
    for rel in sorted(manifest.get("files") or {}):
        top, _, name = rel.partition("/")
        if top.casefold() == "gate" and top != "gate":
            problems.append(f"{rel} is spelled otherwise than gate/")
        elif top != "gate":
            if importable(rel) or "__pycache__" in rel.split("/"):
                problems.append(f"{rel} is code outside gate/")
        elif not GATE_DATA.fullmatch(name):
            if name in sources:
                gate[name] = rel
            else:
                problems.append(f"{rel} is not a file the gate is made of")
    absent = sorted(set(sources) - set(gate))
    if absent:
        problems.append(f"the package's gate lacks {', '.join(absent)}")
    if problems:
        return problems
    ancestor = subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                               commit, "origin/main"], capture_output=True)
    if ancestor.returncode != 0:
        return [f"commit {commit[:12]} is not on this checkout's origin/main (git -C "
                f"{checkout} fetch origin, then run this again; a commit that is not on "
                "WikiLean's main is not one to run)"]
    for name, rel in sorted(gate.items()):
        shown = subprocess.run(["git", "-C", str(checkout), "show", f"{commit}:{sources[name]}"],
                               capture_output=True)
        if shown.returncode != 0 or shown.stdout != (Path(directory) / rel).read_bytes():
            problems.append(f"{rel} is not {sources[name]} at {commit[:12]}")
    return problems


def root_problems(root: Path, manifest: dict) -> list[str]:
    """Code in the root the package did not bring: the two ``sys.path`` directories.

    ``materialise`` writes the package over the root and deletes nothing, so a file left
    in ``root/`` or ``root/gate/`` by an earlier package or by hand would be imported by
    the gate as surely as one the package carried.
    """
    root = Path(root)
    carried = set(manifest.get("files") or {})
    problems: list[str] = []
    for directory, prefix in ((root, ""), (root / "gate", "gate/")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            rel = prefix + path.name
            if path.is_symlink():
                problems.append(f"{rel} is a symlink")
            elif path.is_dir():
                if prefix == "gate/" and path.name != "tasks":
                    problems.append(f"{rel}/ is a directory the gate does not hold")
                elif (prefix == "gate/" or path.name.casefold() != "gate") and any(
                        importable(child.name) for child in path.iterdir()):
                    problems.append(f"{rel}/ holds code, and its parent is on sys.path")
            elif importable(path.name) and rel not in carried:
                problems.append(f"{rel} is code the package did not carry")
            elif prefix == "gate/" and rel not in carried and not GATE_DATA.fullmatch(path.name):
                problems.append(f"{rel} is a file the package did not carry")
    return problems


def checked_root(workdir: Path, target: Path, manifest: dict) -> Path:
    """The package materialised into ``<workdir>/root``, holding no code it did not carry."""
    root = work_mod.materialise(Path(target), Path(workdir))
    problems = root_problems(root, manifest)
    if problems:
        raise ClaimProblem(f"{root} holds code the package did not bring, and the gate would "
                           "import it: " + "; ".join(problems[:5]) + ". Remove it, or use a "
                           "fresh --workdir")
    return root


# ---- the index bundle -------------------------------------------------------------------


def install_index(*, base: str, token: str, workdir: Path, record: dict, cache: Path,
                  execute: bool = False, log=print) -> dict:
    """Download the package's index bundle and link it into the root; a dry run reads its manifest.

    A dry run downloads no package: before the claim's package is unpacked, it plans the
    bundle the claim names.
    """
    manifest: dict = {}
    if execute or (package_dir(workdir, record) / package_mod.PACKAGE_JSON).is_file():
        target, manifest = fetch_package(base=base, token=token, workdir=workdir,
                                         record=record, execute=execute, log=log)
        root = checked_root(workdir, target, manifest)
    sha = (manifest.get("index_bundle") or {}).get("manifest_sha256") or record.get("bundle_sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise ClaimProblem("the package names no index bundle")
    if manifest and record.get("bundle_sha256") not in (None, sha):
        raise ClaimProblem(f"the package names index bundle {sha[:12]} and its claim "
                           f"{str(record.get('bundle_sha256'))[:12]}")
    summary = transport().fetch_bundle(base, token, sha, cache, skip=SKIP_PARTS,
                                       execute=execute, log=log)
    restricted = summary.get("use_restricted") or []
    log(f"  bundle   : {sha[:12]}, {summary['files']} file(s), {summary['bytes']} bytes, "
        f"{summary['missing']} to download into {summary['root']}")
    for row in restricted:
        log(f"  restricted: {json.dumps(row, sort_keys=True)[:300]}")
    if not execute:
        log("# dry run: would download what the cache lacks and link the index and the "
            "search corpus into the root")
        return summary
    bundle_root = Path(summary["root"])
    bundle = json.loads((bundle_root / "MANIFEST.json").read_text(encoding="utf-8"))
    linked = link_index(root, bundle_root, bundle)
    log(f"  linked   : {', '.join(linked)}")
    return {**summary, "linked": linked}


def link_index(root: Path, bundle_root: Path, bundle: dict) -> list[str]:
    """Hard-link (or copy) the bundle's index and search corpus to where the root reads them.

    Only files the bundle's MANIFEST.json lists are read, each by a plain path inside the
    bundle, and the index file's own bytes must hash to the sha the gate pins. It goes
    to the path ``gate.json`` names, which must lie under ``root/index/`` (no package
    carries ``index/``, so nothing writes there through the link) and end as the
    bundle's file does, because the index reader is chosen by suffix. The search corpus
    goes to ``index/search/``, for a run that hands it out.
    """
    root = Path(root).resolve()
    bundle_root = Path(bundle_root).resolve()
    config = work_mod.gate_config(root)
    index = config.get("index") if isinstance(config.get("index"), dict) else {}
    bundled = bundle.get("index") if isinstance(bundle.get("index"), dict) else {}
    files = bundle.get("files") if isinstance(bundle.get("files"), dict) else {}

    def listed(rel: str) -> Path:
        if not (package_mod.plain(rel) and isinstance(files.get(rel), dict)):
            raise ClaimProblem(f"the bundle's MANIFEST.json does not list {rel[:120]!r}")
        source = (bundle_root / rel).resolve()
        if bundle_root not in source.parents:
            raise ClaimProblem(f"{rel[:120]!r} is not inside the bundle")
        return source

    linked: list[str] = []
    if index.get("path"):
        pinned = index.get("sha256")
        source = listed(str(bundled.get("path") or ""))
        if not (isinstance(pinned, str) and bundled.get("sha256") == pinned
                and files[str(bundled["path"])].get("sha256") == pinned):
            raise ClaimProblem("the bundle's index is not the one the gate pins")
        if source.is_file() and keys_mod.sha256_file(source) != pinned:
            raise ClaimProblem(f"{source} does not hash to the index the gate pins")
        named = Path(str(index["path"]))
        destination = (named if named.is_absolute() else root / "gate" / named).resolve()
        if (root / "index") not in destination.parents:
            raise ClaimProblem(f"gate.json names the index at {index['path']}, outside "
                               f"{root / 'index'}")
        if destination.suffix != source.suffix:
            raise ClaimProblem(f"gate.json names the index as {destination.name} and the "
                               f"bundle's is {source.name}; the reader is chosen by suffix")
        _place(source, destination)
        linked.append(destination.relative_to(root).as_posix())
    _use_candidates, use_search = work_mod.hands_out(root)
    if use_search:
        for name in workdir_mod.SEARCH_FILES:
            _place(listed(f"search/{name}"), work_mod.search_dir(root) / name)
            linked.append(f"index/search/{name}")
    return linked


def _place(source: Path, destination: Path) -> None:
    """A hard link where the filesystem allows it, else a copy; never a half-written file."""
    if not Path(source).is_file():
        raise ClaimProblem(f"the bundle holds no {source.name} at {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name(f".{destination.name}.linking")
    if scratch.exists() or scratch.is_symlink():
        scratch.unlink()
    try:
        os.link(source, scratch)
    except OSError:
        shutil.copyfile(source, scratch)
    os.replace(scratch, destination)


# ---- the lease, the receipt and the end ----------------------------------------------


def _on_claim(base: str, token: str, workdir: Path, mine: dict, action: str,
              payload: dict | None = None) -> dict:
    """A request about the held claim; a refusal that says it ended is recorded so."""
    try:
        return _call(base, f"/api/claims/{mine['claim_id']}/{action}", token, payload)
    except ClaimProblem as exc:
        if getattr(exc, "code", "") in ENDED_CODES:
            save(workdir, {**_as_claim(mine), "state": "ended"}, base)
        raise


def ended(exc: BaseException) -> bool:
    """Whether a refusal says the claim is over, rather than not now."""
    return getattr(exc, "code", "") in ENDED_CODES


def renew(*, base: str, token: str, workdir: Path, log=print) -> dict:
    """Extend the lease; a claim that has ended is recorded so and raises ClaimProblem."""
    mine = held(workdir)
    if not live(mine):
        raise ClaimProblem("this workdir holds no live claim to renew")
    answer = _on_claim(base, token, workdir, mine, "renew", {})
    record = save(workdir, answer["claim"], base)
    log(f"  renewed  : {record['claim_id']}, lease until {stamp(record.get('lease_until'))}")
    return record


def _as_claim(record: dict) -> dict:
    """A claim.json record in the API's own shape, for :func:`save`."""
    return {"id": record.get("claim_id"), "run_id": record.get("run_id"),
            "agent_id": record.get("agent_id"), "state": record.get("state"),
            "claimed_at": record.get("claimed_at"), "lease_until": record.get("lease_until"),
            "expires_at": record.get("expires_at"), "ended_at": record.get("ended_at"),
            "end_reason": record.get("end_reason"), "package_url": record.get("package_url"),
            "bundle_url": record.get("bundle_url"),
            "package": {key: record.get(key) for key in ("envelope_sha256", "bytes",
                                                         "bundle_sha256", "campaign",
                                                         "index_sha256")}}


def renewer(*, base: str, token: str, workdir: Path, log=print,
            every: float | None = None, clock=time.monotonic):
    """The after-task hook of a paid run: renew, and stop the run once the claim has ended.

    It renews when ``every`` seconds have passed since the last renewal (the run renews
    once before it starts), because each renewal spends the agent's write budget and the
    lease is a day long. A refusal that says the claim ended stops the run after the task
    in hand, and what passed is still harvested; any other refusal or a network failure
    is logged and the run goes on, and the next task tries again.
    """
    every = RENEW_EVERY if every is None else every
    last = [clock()]

    def after_task(_record: dict) -> bool:
        if clock() - last[0] < every:
            return True
        try:
            renew(base=base, token=token, workdir=workdir, log=log)
            last[0] = clock()
        except ClaimProblem as exc:
            if ended(exc):
                log(f"  stopped  : {exc}; the tasks that passed are harvested, the rest "
                    "are not run")
                return False
            log(f"  warning  : could not renew the claim ({exc}); carrying on")
        except ValueError as exc:  # contrib_transport.TransportError: the network
            log(f"  warning  : could not renew the claim ({exc}); carrying on")
        return True
    return after_task


def link(*, base: str, token: str, workdir: Path, submission_id: str, log=print) -> dict:
    """Join an intake receipt to the claim, so the coordinator finds it."""
    mine = held(workdir)
    if not live(mine):
        raise ClaimProblem("this workdir holds no live claim to link a receipt to")
    answer = _on_claim(base, token, workdir, mine, "submissions",
                       {"submission_id": submission_id})
    log(f"  linked   : {submission_id} to {mine['claim_id']}")
    return answer


def receipts_dir(workdir: Path, claim_id: str) -> Path:
    """``<workdir>/submissions/<claim id>/``: the receipts of one claim's sets."""
    if not re.fullmatch(r"clm-[0-9a-f]{32}", str(claim_id)):
        raise ClaimProblem(f"{claim_id!r} is not a claim id")
    return Path(workdir) / SUBMISSIONS_DIR / claim_id


def save_receipt(workdir: Path, claim_id: str, receipt: dict) -> Path | None:
    """Keep each submission's receipt by its claim and id; SUBMISSION.json is the latest."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    (workdir / "SUBMISSION.json").write_text(text, encoding="utf-8")
    sub = (receipt.get("submission") or {}).get("id")
    if isinstance(sub, str) and re.fullmatch(r"sub-[0-9a-f]{64}", sub):
        path = receipts_dir(workdir, claim_id) / f"{sub}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    return None


def release(*, base: str, token: str, workdir: Path, reason: str, note: str | None = None,
            execute: bool = False, log=print) -> dict | None:
    """End the claim with a reason: ``done`` (a receipt is linked), or why not."""
    if reason not in RELEASE_REASONS:
        raise ClaimProblem(f"a claim is released as one of {', '.join(RELEASE_REASONS)}")
    mine = held(workdir)
    if not live(mine):
        raise ClaimProblem("this workdir holds no live claim to release")
    if not execute:
        log(f"# dry run: would release {mine['claim_id']} on {mine['run_id']} as {reason}")
        return None
    body = {"reason": reason, **({"note": note} if note else {})}
    answer = _on_claim(base, token, workdir, mine, "release", body)
    record = save(workdir, answer["claim"], base)
    log(f"  released : {record['claim_id']} ({record.get('state')}, {reason})")
    return record
