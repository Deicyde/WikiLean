#!/usr/bin/env python3
"""The coordinator's side of contribute mode: publish one planned run as a package.

docs/PIPELINE.md section 3 (stage 11). Copies a planned locate run into a package,
writes its manifest, and either commits it to the work repo or writes it as one
envelope for the claims API::

    python3 scripts/pipeline/contribute/publish_package.py --run-id loc-7 \\
        --plan <planned-run dir> --sources <sources.jsonl> --universe <universe.json> \\
        --repo <work-repo clone> [--mode locate] [--max-cost 20] [--no-commit]

    python3 scripts/pipeline/contribute/publish_package.py --run-id loc-7 \\
        --plan <planned-run dir> --sources <sources.jsonl> --universe <universe.json> \\
        --sources-dir <sourceset>/sources --candidates-dir <candidates> \\
        --inventory-corpus <corpus>/<inventory run> --bundle-dir <index bundle> \\
        --producer-commit <40 hex> --envelope <loc-7.json> [--created <ISO 8601 Z>]
    python3 scripts/articles/upload_contrib.py package <loc-7.json> \
        --bucket wikilean-contrib --account-id <32 hex> --execute
    python3 scripts/pipeline/contribute/publish_package.py --register <loc-7.json> --execute
    python3 scripts/pipeline/contribute/publish_package.py --set-state loc-7 retired --execute

The plan's paths are rooted at ``{{WIKILEAN_CONTRIB_ROOT}}``; a package that still names
a directory of this machine is refused, never rewritten. ``--sources-dir``,
``--candidates-dir`` and ``--inventory-corpus`` ship each source's fetched bytes, its
candidates and the inventory run, each checked first (``package.py``): a source
renders to the plan's pins, its candidates were made from that rendering and the
gate's index, and the inventory is a complete, gated harvest of the plan's inventory
run over the same renderings. Without them the contributor refetches and regenerates.

**The claims API's packages** are envelopes (``envelope.py``). ``--envelope`` builds one
in a scratch directory and writes it; it names the index bundle the contributor
downloads (``--bundle-dir``, checked against the plan's universe and index) and the
campaign a contributor seals under, ``run:<run id>``, unchallenged, as WikiLean's own
waves now are (Jack, 2026-11-12, "Waves, no challenge"; the seal's ``--unchallenged``).
Everything the Worker's ``POST /api/packages`` checks is checked before the envelope
is written. The upload is ``upload_contrib.py``'s, and only then can ``--register``
publish it: the Worker refuses
a package the bucket does not hold. A registered package is never replaced, so register
the envelope file you uploaded, not a rebuild of it. ``--register`` and ``--set-state``
read ``PIPELINE_TOKEN`` from the environment, and print what they would send unless
``--execute``.

Exit codes: 0 published, 2 the package was refused, 3 the publisher failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTICLES = HERE.parents[1] / "articles"
for _path in (str(HERE), str(ARTICLES)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import envelope as envelope_mod  # noqa: E402
import package as package_mod  # noqa: E402
import workrepo  # noqa: E402

TOKEN_ENV = "PIPELINE_TOKEN"
STATES = ("open", "done", "retired")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
CREATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
#: What the claims API takes (wiki/src/claims.ts parsePackage).
MODE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
#: A bearer token is visible ASCII: http.client sends headers as Latin-1, and its
#: encoding error would carry the whole token in its repr.
TOKEN_RE = re.compile(r"[!-~]+")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def publish(*, run_id: str, plan: Path, sources: Path, universe: Path, repo: Path,
            mode: str = "locate", max_cost: float | None = None,
            producer_commit: str | None = None, corpus_sha256: str | None = None,
            tasks: list[str] | None = None, inputs: list[Path] | None = None,
            recorded_argv: list[str] | None = None, commit: bool = True,
            sources_dir: Path | None = None, candidates_dir: Path | None = None,
            inventory_dir: Path | None = None) -> dict:
    """Write and (by default) commit the package; returns its manifest."""
    clone = workrepo.check_clone(repo)
    manifest = package_mod.build(
        Path(plan), run_id=run_id, repo=clone,
        sources=read_jsonl(sources),
        universe=json.loads(Path(universe).read_text(encoding="utf-8")),
        mode=mode, tasks=tasks, max_cost=max_cost, producer_commit=producer_commit,
        corpus_sha256=corpus_sha256, inputs=inputs, recorded_argv=recorded_argv,
        extra_roots=[str(Path(plan).resolve().parent)], sources_dir=sources_dir,
        candidates_dir=candidates_dir, inventory_dir=inventory_dir)
    rel = f"{workrepo.PACKAGES}/{run_id}"
    if commit:
        head = workrepo.commit_paths(
            clone, [rel],
            f"contribute: package locate run {run_id} ({mode}, "
            f"{len(manifest['tasks'])} task(s))")
        manifest = {**manifest, "commit": head}
    return manifest


def read_bundle(bundle_dir: Path, universe: dict, plan: Path) -> tuple[str, dict]:
    """``(sha256 of MANIFEST.json, the manifest)`` of an index bundle this plan can use.

    The contributor downloads the bundle by that sha (``contrib_transport.fetch_bundle``)
    and installs its index where the gate reads it, so it must be of the plan's universe
    and, when the gate pins an index, be that index.
    """
    import contrib_transport  # noqa: PLC0415 — the client's schema is the one to meet

    path = Path(bundle_dir) / contrib_transport.MANIFEST
    try:
        raw = path.read_bytes()
    except OSError:
        raise package_mod.PackageProblem(f"no index bundle at {bundle_dir}: it holds no "
                                         f"{contrib_transport.MANIFEST}") from None
    sha = hashlib.sha256(raw).hexdigest()
    try:
        manifest = contrib_transport.read_manifest(raw, sha)
    except contrib_transport.TransportError as exc:
        raise package_mod.PackageProblem(f"{path}: {exc}") from None
    have = (manifest.get("universe") or {}).get("universe_sha256")
    if have != universe.get("universe_sha256"):
        raise package_mod.PackageProblem(
            f"the index bundle is of universe {str(have)[:12]} and the plan of "
            f"{str(universe.get('universe_sha256'))[:12]}")
    gate = package_mod.plan_json(plan, "gate/gate.json")
    planned = (gate.get("universe") or {}).get("universe_sha256") \
        if isinstance(gate.get("universe"), dict) else None
    if planned != universe.get("universe_sha256"):
        raise package_mod.PackageProblem(
            f"the plan was made for universe {str(planned)[:12]} and --universe is "
            f"{str(universe.get('universe_sha256'))[:12]}")
    pinned = (gate.get("index") or {}).get("sha256") if isinstance(gate.get("index"), dict) \
        else None
    bundled = (manifest.get("index") or {}).get("sha256")
    if pinned and bundled != pinned:
        raise package_mod.PackageProblem(
            f"the index bundle carries index {str(bundled)[:12]} and the plan's gate pins "
            f"{str(pinned)[:12]}")
    return sha, manifest


def publish_envelope(*, run_id: str, plan: Path, sources: Path, universe: Path, out: Path,
                     sources_dir: Path, bundle_dir: Path, producer_commit: str,
                     candidates_dir: Path | None = None, inventory_dir: Path | None = None,
                     mode: str = "locate", max_cost: float | None = None,
                     corpus_sha256: str | None = None, tasks: list[str] | None = None,
                     inputs: list[Path] | None = None,
                     recorded_argv: list[str] | None = None,
                     created: str | None = None) -> dict:
    """Build the package in a scratch directory and write its envelope to ``out``.

    Returns its manifest with ``envelope`` (path, sha256, bytes) beside it. The tasks
    are the plan's unless named: the claims API counts them, and a package of none is
    no package.
    """
    if not COMMIT_RE.fullmatch(producer_commit or ""):
        raise package_mod.PackageProblem(
            "an envelope needs --producer-commit, the 40-hex commit of the checkout the "
            "plan was made at: the package's gate code runs on a contributor's machine, "
            "and this names the public commit it comes from")
    if created is not None and not CREATED_RE.fullmatch(created):
        raise package_mod.PackageProblem(f"--created {created!r} is not ISO 8601 UTC "
                                         "ending in Z")
    plan = Path(plan)
    if not MODE_RE.fullmatch(mode or ""):
        raise package_mod.PackageProblem(f"{mode!r} is not a package mode the claims API "
                                         "takes (lowercase, at most 32 characters)")
    planned = package_mod.plan_json(plan, "run.json").get("mode") \
        if (plan / "run.json").is_file() else None
    if planned is not None and planned != mode:
        raise package_mod.PackageProblem(f"the plan is a {planned!r} run and --mode says "
                                         f"{mode!r}")
    universe_doc = json.loads(Path(universe).read_text(encoding="utf-8"))
    bundle_sha, bundle = read_bundle(bundle_dir, universe_doc, plan)
    if not tasks:
        tasks = sorted(path.stem for path in (plan / "tasks").glob("*.json"))
    if not tasks:
        raise package_mod.PackageProblem(f"{plan} plans no task; a package of none is no "
                                         "package")
    with tempfile.TemporaryDirectory(prefix="wikilean-package-") as scratch:
        manifest = package_mod.build(
            plan, run_id=run_id, repo=Path(scratch), sources=read_jsonl(sources),
            universe=universe_doc, mode=mode, tasks=tasks, max_cost=max_cost,
            producer_commit=producer_commit,
            corpus_sha256=corpus_sha256 or (bundle.get("index") or {}).get("sha256"),
            inputs=inputs, recorded_argv=recorded_argv,
            extra_roots=[str(plan.resolve().parent)], sources_dir=sources_dir,
            candidates_dir=candidates_dir, inventory_dir=inventory_dir,
            index_bundle=bundle_sha,
            seal={"campaign": f"run:{run_id}", "unchallenged": True}, created=created)
        data = envelope_mod.encode(package_mod.package_dir(Path(scratch), run_id))
    registration(data)
    sha, size = envelope_mod.save(data, out)
    return {**manifest, "envelope": {"path": str(out), "sha256": sha, "bytes": size}}


def registration(data: bytes) -> dict:
    """What ``POST /api/packages`` takes for this envelope, held to the Worker's own rules.

    The body ``parsePackage`` (wiki/src/claims.ts) reads, refused here for any reason it
    would refuse: the bucket never replaces an object, so an envelope uploaded and then
    refused is an orphan.
    """
    run_id, files = envelope_mod.decode(data)
    manifest = json.loads(files[package_mod.PACKAGE_JSON])
    gate = json.loads(files["gate/gate.json"]) if "gate/gate.json" in files else {}
    index = gate.get("index") if isinstance(gate.get("index"), dict) else {}
    body = {"run_id": run_id, "envelope_sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "bundle_sha256": (manifest.get("index_bundle") or {}).get("manifest_sha256"),
            "universe_sha256": (manifest.get("universe") or {}).get("universe_sha256"),
            "index_sha256": index.get("sha256"),
            "campaign": (manifest.get("seal") or {}).get("campaign"),
            "mode": manifest.get("mode"), "tasks": len(manifest.get("tasks") or []),
            "sources": len(manifest.get("sources") or []),
            "max_cost": manifest.get("max_cost")}
    problems = [f"{key} is not 64 lowercase hex" for key in ("bundle_sha256", "universe_sha256")
                if not isinstance(body[key], str) or not SHA_RE.fullmatch(body[key])]
    if body["index_sha256"] is not None and not (
            isinstance(body["index_sha256"], str) and SHA_RE.fullmatch(body["index_sha256"])):
        problems.append("the gate's index sha256 is not 64 lowercase hex")
    campaign, own = body["campaign"], f"run:{run_id}"
    challenge = campaign[len(own) + 1:] if isinstance(campaign, str) \
        and campaign.startswith(own + "+") else None
    if campaign != own and not (challenge and _is_run_id(challenge)):
        problems.append(f"the campaign is {campaign!r}, not {own} or {own}+<challenge run>")
    if not isinstance(body["mode"], str) or not MODE_RE.fullmatch(body["mode"]):
        problems.append(f"{body['mode']!r} is not a package mode")
    problems += [f"it has no {key}" for key in ("tasks", "sources") if body[key] < 1]
    cost = body["max_cost"]
    if cost is not None and not (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                                 and math.isfinite(cost) and cost >= 0):
        problems.append(f"its spend cap {cost!r} is not a non-negative number")
    if problems:
        raise package_mod.PackageProblem(f"the claims API would refuse the package of "
                                         f"{run_id}: " + "; ".join(problems))
    return body


def _is_run_id(value: str) -> bool:
    try:
        package_mod.check_run_id(value)
    except package_mod.PackageProblem:
        return False
    return True


def _send(path: str, body: dict, base: str | None) -> dict:
    """One call to the claims API as the pipeline bearer; a refusal is a PackageProblem."""
    import contrib_transport  # noqa: PLC0415 — the only network this command has

    token = os.environ.get(TOKEN_ENV, "")
    if not TOKEN_RE.fullmatch(token):
        raise package_mod.PackageProblem(f"{TOKEN_ENV} is not set, or holds more than "
                                         "visible ASCII; the claims API takes packages from "
                                         "the pipeline bearer only")
    try:
        return contrib_transport.call(base or contrib_transport.BASE_URL, path, token, body)
    except contrib_transport.Refusal as exc:
        raise package_mod.PackageProblem(f"the claims API refused it: {exc}") from None


def register(envelope: Path, *, base: str | None = None, execute: bool = False,
             log=print) -> dict:
    """Publish an uploaded envelope through ``POST /api/packages``; a dry run prints it."""
    body = registration(Path(envelope).read_bytes())
    if not execute:
        log(f"# dry run: would POST /api/packages to {base or 'the live site'}")
        log(json.dumps(body, indent=1, sort_keys=True))
        return {"ok": True, "dry_run": True, "body": body}
    return _send("/api/packages", body, base)


def set_state(run_id: str, state: str, *, base: str | None = None, execute: bool = False,
              log=print) -> dict:
    """Move a published package: ``open``, ``done`` or ``retired`` (ends a live claim)."""
    package_mod.check_run_id(run_id)
    if state not in STATES:
        raise package_mod.PackageProblem(f"a package's state is one of {', '.join(STATES)}")
    path = f"/api/packages/{run_id}/state"
    if not execute:
        log(f"# dry run: would POST {path} {json.dumps({'state': state})} to "
            f"{base or 'the live site'}")
        return {"ok": True, "dry_run": True}
    return _send(path, {"state": state}, base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id")
    parser.add_argument("--plan", type=Path,
                        help="the planner's output directory, rooted at the placeholder")
    parser.add_argument("--sources", type=Path,
                        help="the sourceset entries, as JSONL (pins only, no bytes)")
    parser.add_argument("--universe", type=Path)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--repo", type=Path, help="a clone of the work repo")
    where.add_argument("--envelope", type=Path, metavar="FILE",
                       help="write the package as one envelope for the claims API, here")
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
    parser.add_argument("--sources-dir", type=Path, default=None,
                        help="the sourceset's source directories: ships each pinned "
                             "source's source.raw.html, rendered and checked first")
    parser.add_argument("--candidates-dir", type=Path, default=None,
                        help="ships each pinned source's candidates (with --sources-dir)")
    parser.add_argument("--inventory-corpus", type=Path, default=None,
                        help="the harvested corpus of the inventory run the plan was cut "
                             "from; ships it for the contributor's seal")
    parser.add_argument("--bundle-dir", type=Path, default=None,
                        help="with --envelope: the index bundle the contributor downloads")
    parser.add_argument("--created", default=None,
                        help="with --envelope: the package's creation time, ISO 8601 UTC "
                             "ending in Z (default: now)")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--register", type=Path, metavar="FILE", default=None,
                        help="publish an uploaded envelope through the claims API")
    parser.add_argument("--set-state", nargs=2, metavar=("RUN_ID", "STATE"), default=None,
                        help=f"move a published package: {', '.join(STATES)}")
    parser.add_argument("--base-url", default=None,
                        help="with --register or --set-state (default: the live site)")
    parser.add_argument("--execute", action="store_true",
                        help="with --register or --set-state: send it; without, print it")
    args = parser.parse_args(argv)
    try:
        if args.register is not None or args.set_state is not None:
            if args.register is not None and args.set_state is not None:
                parser.error("--register and --set-state are two commands")
            if args.register is not None:
                answer = register(args.register, base=args.base_url, execute=args.execute)
            else:
                answer = set_state(*args.set_state, base=args.base_url, execute=args.execute)
            if not answer.get("dry_run"):
                print(json.dumps(answer, indent=1, sort_keys=True))
            return 0
        missing = [flag for flag, value in (("--run-id", args.run_id), ("--plan", args.plan),
                                            ("--sources", args.sources),
                                            ("--universe", args.universe)) if not value]
        if missing or (args.repo is None and args.envelope is None):
            parser.error(f"publishing needs {', '.join(missing or ['--repo or --envelope'])}")
        if args.envelope is not None:
            if args.sources_dir is None or args.bundle_dir is None:
                parser.error("--envelope needs --sources-dir and --bundle-dir: a claims "
                             "package ships its sources' bytes and names its index bundle")
            manifest = publish_envelope(
                run_id=args.run_id, plan=args.plan, sources=args.sources,
                universe=args.universe, out=args.envelope, sources_dir=args.sources_dir,
                bundle_dir=args.bundle_dir, producer_commit=args.producer_commit or "",
                candidates_dir=args.candidates_dir, inventory_dir=args.inventory_corpus,
                mode=args.mode, max_cost=args.max_cost, corpus_sha256=args.corpus_sha256,
                tasks=args.tasks, inputs=args.inputs, recorded_argv=args.recorded_argv,
                created=args.created)
        else:
            manifest = publish(run_id=args.run_id, plan=args.plan, sources=args.sources,
                               universe=args.universe, repo=args.repo, mode=args.mode,
                               max_cost=args.max_cost, producer_commit=args.producer_commit,
                               corpus_sha256=args.corpus_sha256, tasks=args.tasks,
                               inputs=args.inputs, recorded_argv=args.recorded_argv,
                               commit=not args.no_commit, sources_dir=args.sources_dir,
                               candidates_dir=args.candidates_dir,
                               inventory_dir=args.inventory_corpus)
    except (package_mod.PackageProblem, workrepo.WorkRepoProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        # the type and the message, never the repr: an encoding error's repr carries
        # the whole string it failed on
        print(f"publishing failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    shipped = any(rel.startswith(f"{package_mod.SOURCES}/") for rel in manifest["files"])
    print(f"packaged {manifest['run_id']} ({manifest['mode']}): "
          f"{len(manifest['sources'])} source(s) pinned by revision and sha"
          f"{', their fetched bytes shipped' if shipped else ''}, "
          f"{len(manifest['tasks'])} task(s), {len(manifest['files'])} file(s)")
    print(f"  universe : {manifest['universe']['id']} "
          f"{str(manifest['universe']['universe_sha256'])[:12]}")
    if "envelope" in manifest:
        wrote = manifest["envelope"]
        print(f"  envelope : {wrote['path']} ({wrote['bytes']} bytes, sha256 "
              f"{wrote['sha256']})")
        print(f"  bundle   : {manifest['index_bundle']['manifest_sha256']}")
        print(f"# next: scripts/articles/upload_contrib.py package {wrote['path']} --bucket "
              "wikilean-contrib --account-id <32 hex> --execute, then this command's "
              f"--register {wrote['path']} --execute")
    elif "commit" in manifest:
        print(f"  committed: {manifest['commit'][:12]}; push it, then upload the declaration "
              "index once per universe as a release asset of the work repo")
    else:
        print("  not committed (--no-commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
