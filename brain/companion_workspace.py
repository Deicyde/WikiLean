"""Private local workspace connecting Autoform, Literature Links and WikiLean.

Configuration and generation reports are operational files, never Literature Links schemas.
Run `python scripts/companion-workspace.py --help` for the setup entry point.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "wikilean-local-companion-workspace/1"


class WorkspaceError(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"


def _run(arguments, *, cwd=None, timeout=300) -> str:
    environment = None
    if Path(arguments[0]).name == "git":
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run([str(a) for a in arguments], cwd=cwd, capture_output=True,
                            text=True, timeout=timeout, check=False, env=environment)
    if result.returncode:
        raise WorkspaceError(f"{arguments[0]} failed ({result.returncode}):\n{result.stderr[-8000:]}")
    return result.stdout


def _write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf8") as handle:
        handle.write(_json(value))
        handle.flush()
        os.fsync(handle.fileno())


def _configuration(workspace: Path) -> dict:
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf8"))
    if config.get("format") != FORMAT or not config.get("workspace_id"):
        raise WorkspaceError("Not a companion workspace")
    if Path(config["wikilean_root"]).resolve() != ROOT:
        raise WorkspaceError("Use this workspace's configured WikiLean checkout")
    return config


def _revision(root: Path) -> dict:
    """Bind editable code, including newly written files, without exposing diffs."""
    git = ["git", "--no-replace-objects", "-C", root]
    head = _run([*git, "rev-parse", "HEAD"]).strip()
    patch = _run([*git, "diff", "--binary", "HEAD"]).encode()
    untracked = _run([*git, "ls-files", "--others", "--exclude-standard", "-z"])
    manifest = []
    names = sorted(name for name in untracked.split("\0") if name)
    for name in names:
        path = root / name
        if path.is_symlink():
            manifest.append([name, "symlink", hashlib.sha256(os.readlink(path).encode()).hexdigest()])
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            manifest.append([name, "file", digest.hexdigest()])
        else:
            raise WorkspaceError(f"Untracked tool input is not a regular file or symlink: {name}")
    identity = [hashlib.sha256(patch).hexdigest(), manifest]
    return {"commit": head, "dirty": bool(patch or names),
            "changes_sha256": hashlib.sha256(_json(identity).encode()).hexdigest()}


def _tool_revisions(config: dict) -> dict:
    return {name: _revision(Path(config[f"{name}_root"])) for name in ("autoform", "literature_links", "wikilean")}


def _dependency_inputs(config: dict) -> dict:
    paths = {
        "autoform_lock": Path(config["autoform_root"]) / "uv.lock",
        "autoform_package": Path(config["autoform_root"]) / "pyproject.toml",
        "literature_links_package": Path(config["literature_links_root"]) / "pyproject.toml",
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def _check_roots(workspace: Path, config: dict) -> None:
    for name in ("autoform", "literature_links", "wikilean"):
        root = Path(config[f"{name}_root"]).resolve()
        if not root.is_dir():
            raise WorkspaceError(f"Missing {name} checkout: {root}")
        if workspace.is_relative_to(root):
            raise WorkspaceError("Keep the workspace outside the three tool checkouts")
        if config.get("consumer"):
            consumer = Path(config["consumer"]["path"]).resolve()
            if consumer.is_relative_to(root) or root.is_relative_to(consumer):
                raise WorkspaceError("Autoform needs an independent consumer outside the tool checkouts")
    inbox = workspace / "inbox"
    for item in config["shards"]:
        root = Path(item).resolve()
        if root == inbox or root.is_relative_to(inbox):
            continue
        if root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise WorkspaceError("Shard selections must not include the workspace or its generated snapshots; use inbox/")



def initialize(workspace: Path, *, autoform_root: Path, literature_links_root: Path,
               node: str | None = None, shards=(), articles=(), consumer=None,
               repo=None, commit=None, include=None, exclude=(), attach_sources=True) -> dict:
    if workspace.exists() or workspace.is_symlink():
        raise WorkspaceError("Workspace already exists; use setup to finish an earlier initialization")
    if (repo or commit) and not consumer:
        raise WorkspaceError("Repository metadata requires a consumer path")
    config = {
        "format": FORMAT, "workspace_id": uuid.uuid4().hex,
        "autoform_root": str(autoform_root.resolve()), "literature_links_root": str(literature_links_root.resolve()),
        "wikilean_root": str(ROOT), "node": str(Path(node).resolve()) if node else None,
        "shards": [str(workspace / "inbox"), *[str(Path(p).resolve()) for p in shards]],
        "articles": [str(Path(p).resolve()) for p in articles],
        "include": list(include) if include else None, "exclude": list(exclude),
        "consumer": {"path": str(Path(consumer).resolve()), "repo": repo, "commit": commit,
                     "attach_sources": attach_sources} if consumer else None,
    }
    _check_roots(workspace, config)
    workspace.mkdir(parents=True, mode=0o700)
    (workspace / "inbox").mkdir()
    (workspace / "generations").mkdir()
    _write_new(workspace / "workspace.json", config)
    return config


@contextmanager
def _lock(workspace: Path):
    path = workspace / ".workspace.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise WorkspaceError("Workspace is locked by another operation; inspect .workspace.lock before manual recovery") from error
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(_json({"pid": os.getpid()}))
        yield
    finally:
        path.unlink()


def setup(workspace: Path) -> dict:
    """Install the local checkouts into one private Python 3.12 environment."""
    config = _configuration(workspace)
    _check_roots(workspace, config)
    uv = shutil.which("uv")
    if not uv:
        raise WorkspaceError("uv is required for the Autoform locked environment setup")
    with _lock(workspace):
        inputs = _dependency_inputs(config)
        environment = workspace / ".venv"
        python = environment / "bin/python"
        if environment.is_symlink():
            raise WorkspaceError("Workspace environment must not be a symlink")
        if environment.exists():
            existing = json.loads(_run([python, "-c", "import json,sys; print(json.dumps([sys.prefix,list(sys.version_info[:2])]))"]))
            if Path(existing[0]).resolve() != environment.resolve():
                raise WorkspaceError("Existing environment belongs to another directory")
            if existing[1] != [3, 12]:
                raise WorkspaceError("Existing environment is not Python 3.12; choose a fresh workspace")
        else:
            _run([uv, "venv", "--python", "3.12", environment])
        lock = workspace / "autoform-requirements.txt"
        _run([uv, "export", "--project", config["autoform_root"], "--frozen", "--no-dev",
              "--no-emit-project", "--format", "requirements-txt", "--output-file", lock])
        _run([uv, "pip", "sync", "--python", python, lock], timeout=600)
        _run([uv, "pip", "install", "--python", python, "--no-deps", "--editable",
              config["autoform_root"], "--editable", config["literature_links_root"]], timeout=600)
        launcher = workspace / "companion"
        text = "#!/bin/sh\nexec " + " ".join(shlex.quote(str(p)) for p in (
            python, ROOT / "scripts/companion-workspace.py", "--workspace", workspace)) + ' "$@"\n'
        launcher.write_text(text, encoding="utf8")
        launcher.chmod(0o700)
        if _dependency_inputs(config) != inputs:
            raise WorkspaceError("Dependency inputs changed during setup; run setup again")
        receipt = workspace / (".setup-" + uuid.uuid4().hex + ".json")
        _write_new(receipt, {"workspace_id": config["workspace_id"], "dependencies": inputs})
        os.replace(receipt, workspace / "setup.json")
        _run([python, ROOT / "scripts/companion-workspace.py", "--workspace", workspace, "doctor"])
    return {"workspace": str(workspace), "launcher": str(launcher), "environment": str(environment)}


def doctor(workspace: Path) -> dict:
    config = _configuration(workspace)
    _check_roots(workspace, config)
    if sys.version_info[:2] != (3, 12):
        raise WorkspaceError("Use the workspace launcher; this integration requires Python 3.12")
    if not (workspace / "setup.json").is_file():
        raise WorkspaceError("Environment setup is incomplete; run setup")
    receipt = json.loads((workspace / "setup.json").read_text())
    if receipt.get("workspace_id") != config["workspace_id"] or receipt.get("dependencies") != _dependency_inputs(config):
        raise WorkspaceError("Dependency inputs changed or setup is incomplete; run setup")
    import autoform_cli
    import literature_links
    for name, module in (("autoform", autoform_cli), ("literature_links", literature_links)):
        if not Path(module.__file__).resolve().is_relative_to(Path(config[f"{name}_root"]).resolve()):
            raise WorkspaceError(f"The installed {name} package does not come from the configured local checkout; run setup")
    if literature_links.__version__ != "1.0.0":
        raise WorkspaceError("This integration requires Literature Links 1.0.0")
    if not hasattr(__import__("autoform_cli.companions", fromlist=["capture_retained_records"]), "capture_retained_records"):
        raise WorkspaceError("The configured Autoform checkout lacks native companion attachments")
    return {"ready": True, "python": sys.version.split()[0], "literature_links": literature_links.__version__,
            "autoform": importlib.metadata.version("autoform"), "tools": _tool_revisions(config)}


def current_generation(workspace: Path) -> Path | None:
    current = workspace / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise WorkspaceError("current must be this workspace's generation symlink")
    resolved = current.resolve(strict=True)
    if resolved.parent != (workspace / "generations").resolve():
        raise WorkspaceError("Current generation escapes this workspace")
    report = json.loads((resolved / "generation.json").read_text())
    if report.get("workspace_id") != _configuration(workspace)["workspace_id"] or report.get("state") != "ready":
        raise WorkspaceError("Current generation is not a completed snapshot of this workspace")
    return resolved


def _select(workspace: Path, generation: Path) -> None:
    # Validate the old pointer, if any; never replace a user's ordinary file.
    current_generation(workspace)
    temporary = workspace / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(Path("generations") / generation.name, target_is_directory=True)
        os.replace(temporary, workspace / "current")
    finally:
        temporary.unlink(missing_ok=True)


def _same_occurrences(entries, index: Path, catalog: Path) -> None:
    from brain.ingest.companion import iter_annotations

    def identity(annotation, origin):
        # Ignore the archive's human-readable path alias, not the URI base,
        # pointer or physical line identifying an occurrence.
        return json.dumps([annotation, origin], sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    expected = Counter(identity(entry["annotation"], entry["origin"]) for entry in entries)
    indexed = Counter(identity(row["annotation"], row["origin"]) for row in iter_annotations(index))
    archived = Counter()
    with (catalog / "records.jsonl").open(encoding="utf8") as handle:
        for line in handle:
            row = json.loads(line)
            archived[identity(row["annotation"], row["origin"])] += 1
    if expected != indexed or expected != archived:
        raise WorkspaceError("SQLite and the Autoform source catalog do not contain the same captured occurrences")


def _article_entries(path: Path) -> tuple[list[dict], dict]:
    """Capture native stored links once. No annotation fields are synthesized."""
    from literature_links import companion
    article = companion.loads(path.read_text(encoding="utf8"))
    if not isinstance(article, dict) or article.get("schema_version", article.get("schemaVersion")) != 4:
        raise WorkspaceError(f"{path}: explicit native migration required before synchronization")
    items = article.get("annotations")
    if not isinstance(items, list):
        raise WorkspaceError(f"{path}: expected a native article annotations array")
    spec = importlib.util.spec_from_file_location("wikilean_native_model", ROOT / "site/native_annotations.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    title = article.get("wikipedia_title", article.get("wikipediaTitle"))
    revid = article.get("revid")
    try:
        native.wikipedia_source_uri(title, revid)
        native.validate_items([item for item in items if item is not None], title=title, revid=revid)
    except (ValueError, TypeError, AttributeError) as error:
        raise WorkspaceError(f"{path}: invalid native article: {error}") from error
    entries = []
    for i, item in enumerate(items):
        if item is None:  # Public API redaction of a rejected work item.
            continue
        if not isinstance(item, dict) or not isinstance(item.get("links"), list) or not isinstance(item.get("unlinked"), list):
            raise WorkspaceError(f"{path}: invalid native work item {i}")
        if any(key in item for key in ("anchor", "anchors", "mathlib", "decl", "module")):
            raise WorkspaceError(f"{path}: mixed native and legacy work item {i}")
        if item.get("status") == "rejected":
            continue
        for j, link in enumerate(item["links"]):
            if not isinstance(link, dict) or not isinstance(link.get("annotation"), dict):
                raise WorkspaceError(f"{path}: invalid native annotation {i}/{j}")
            # The stored link plus the item's review facts and the article's
            # website pin, exactly as the Worker's companion routes serve it.
            annotation = native.companion_annotation(item, link, title=title, revid=revid,
                                                     wikidata_qid=article.get("wikidata_qid"))
            if companion.check(annotation).problems:
                raise WorkspaceError(f"{path}: invalid native annotation {i}/{j}")
            base = link.get("base_uri")
            if base is None:
                import re
                references = [annotation["source"]["uri"], *([annotation["$schema"]] if "$schema" in annotation else [])]
                if native.target_uri(annotation) is not None:
                    references.append(native.target_uri(annotation))
                if any(not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", reference) for reference in references):
                    raise WorkspaceError(f"{path}: relative native references require their original base_uri")
                base = path.as_uri()
            entries.append({"annotation": annotation, "origin": {
                "base_uri": base, "pointer": f"/annotations/{i}/links/{j}/annotation"}})
    return entries, {"producer": "wikilean-native", "article": str(path), "annotations": len(entries)}


def _prepare_native_attachment(workspace: Path, config: dict):
    """Register one stable pointer; subsequent selections update both readers."""
    from autoform_cli.runtime import resolve_runtime_paths
    from autoform_cli.companions import load_project_companions
    root = resolve_runtime_paths(config["consumer"]["path"]).project_root
    path = root / ".autoform-companions.json"
    if path.is_symlink():
        raise WorkspaceError("Refusing to replace a symlinked companion project configuration")
    before = path.read_bytes() if path.exists() else None
    load_project_companions(root)  # Validate authored selections before changing authority.
    value = json.loads(before) if before is not None else {}
    if not isinstance(value, dict) or not isinstance(value.get("archives", []), list):
        raise WorkspaceError("Invalid existing Autoform companion configuration")
    archive = str(workspace / "current/autoform-native/records.jsonl")
    if archive in value.get("archives", []):
        return None
    value["archives"] = [*value.get("archives", []), archive]
    temporary = path.with_name(".autoform-companions-" + uuid.uuid4().hex + ".tmp")
    _write_new(temporary, value)
    return path, temporary, before


def _install_native_attachment(prepared) -> None:
    if prepared is None:
        return
    path, temporary, before = prepared
    if path.is_symlink() or (path.read_bytes() if path.exists() else None) != before:
        raise WorkspaceError("Autoform configuration changed after preflight; selected data is intact, retry sync")
    os.replace(temporary, path)


def synchronize(workspace: Path) -> dict:
    from autoform_cli.companions import capture_retained_records, write_retained_annotations
    from literature_links.discovery import collect
    from brain.ingest.companion import import_retained

    config = _configuration(workspace)
    with _lock(workspace):
        checked = doctor(workspace)
        current_generation(workspace)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        generation = workspace / "generations" / name
        generation.mkdir(mode=0o700)
        archive = generation / "autoform-native"
        prepared_attachment = None
        try:
            if config["consumer"] and config["consumer"]["attach_sources"]:
                prepared_attachment = _prepare_native_attachment(workspace, config)
            report = {"workspace_id": config["workspace_id"], "generation": name, "state": "building",
                      "tools": checked["tools"], "python": checked["python"], "producer_reports": []}
            selection = collect(config["shards"], include=config["include"], exclude=config["exclude"])
            problems = [p for p in selection.problems if p.code != "NO_ANNOTATIONS"]
            if problems:
                raise WorkspaceError("; ".join(f"{p.code}: {p.message}" for p in problems))
            entries = []
            for record in selection.records:
                origin = {"base_uri": record.path.as_uri(), "pointer": record.pointer}
                if record.line is not None:
                    origin["line"] = record.line
                entries.append({"annotation": record.annotation, "origin": origin})
            for article in config["articles"]:
                article_records, producer_report = _article_entries(Path(article).resolve())
                entries.extend(article_records)
                report["producer_reports"].append(producer_report)
            if not entries:
                raise WorkspaceError("No annotations selected")
            # Both consumers receive the same detached, reference-validated values.
            captured = capture_retained_records(entries)
            entries = [record.entry() for record in captured.records]
            index_report = import_retained(entries, source_key="companion-workspace", output=generation / "index")
            archive_report = write_retained_annotations(captured, output=archive)
            _same_occurrences(entries, generation / "index", archive)
            if _tool_revisions(config) != checked["tools"]:
                raise WorkspaceError("A tool checkout changed during sync; retry with stable code")
            report.update(state="ready", annotations=len(entries), files=len(selection.files) + len(config["articles"]),
                          index=index_report, catalog=archive_report, catalog_path=str(archive),
                          consumer=config["consumer"],
                          packages={d.metadata["Name"]: d.version for d in importlib.metadata.distributions()})
            _write_new(generation / "generation.json", report)
            _select(workspace, generation)
            _install_native_attachment(prepared_attachment)
            return report
        except BaseException as error:
            try:
                selected = current_generation(workspace) == generation
            except (OSError, ValueError):
                selected = None
            marker = "interruption.json" if selected is not False else "failure.json"
            _write_new(generation / marker, {"error": str(error), "selected": selected})
            raise
        finally:
            if prepared_attachment is not None:
                prepared_attachment[1].unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=lambda p: Path(p).expanduser().resolve())
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create and install a private local workspace")
    init.add_argument("--autoform-root", required=True, type=Path)
    init.add_argument("--literature-links-root", required=True, type=Path)
    init.add_argument("--node")
    init.add_argument("--shards", action="append", default=[])
    init.add_argument("--article", action="append", default=[])
    init.add_argument("--include", action="append")
    init.add_argument("--exclude", action="append", default=[])
    init.add_argument("--consumer", type=Path)
    init.add_argument("--repo")
    init.add_argument("--commit")
    init.add_argument("--no-attach-sources", action="store_true")
    for name in ("setup", "doctor", "sync", "status"):
        commands.add_parser(name)
    serve = commands.add_parser("serve", help="browse the active snapshot on this computer")
    serve.add_argument("--port", type=int, default=8743)
    query = commands.add_parser("query", help="read complete records from the active SQLite snapshot")
    query.add_argument("--source")
    query.add_argument("--target")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.workspace, autoform_root=args.autoform_root, literature_links_root=args.literature_links_root,
                       node=args.node, shards=args.shards, articles=args.article, consumer=args.consumer,
                       repo=args.repo, commit=args.commit, include=args.include, exclude=args.exclude,
                       attach_sources=not args.no_attach_sources)
            result = setup(args.workspace)
        elif args.command == "setup":
            result = setup(args.workspace)
        elif args.command == "doctor":
            result = doctor(args.workspace)
        elif args.command == "sync":
            completed = synchronize(args.workspace)
            result = {key: completed[key] for key in ("generation", "annotations", "catalog_path", "producer_reports")}
            result["report"] = str(args.workspace / "generations" / completed["generation"] / "generation.json")
        else:
            selected = current_generation(args.workspace)
            if args.command == "status":
                result = {"workspace": str(args.workspace), "current": str(selected) if selected else None,
                          "locked": (args.workspace / ".workspace.lock").exists()}
                if selected:
                    snapshot = json.loads((selected / "generation.json").read_text())
                    result.update({key: snapshot[key] for key in ("annotations", "catalog_path", "producer_reports")})
            elif selected is None:
                raise WorkspaceError("No active snapshot; run sync first")
            elif args.command == "query":
                from brain.ingest.companion import iter_annotations
                for row in iter_annotations(selected / "index", source=args.source, target=args.target):
                    print(json.dumps(row, ensure_ascii=True))
                return 0
            else:
                from brain.companion_server import create_server
                server = create_server(args.workspace / "current/index", port=args.port,
                                       generation_root=args.workspace / "generations")
                print(f"Companion workspace: http://127.0.0.1:{server.server_port}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                return 0
        print(_json(result), end="")
        return 0
    except (WorkspaceError, OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        print(f"companion workspace: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
