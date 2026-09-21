"""Offline evidence contract for official Mathlib source and doc-gen artifacts.

Acquisition records fresh requests, never retroactively certifies retained files.
Normalization checks GitHub's artifact digest, job/run association, the linked
Mathlib revision, and the complete Git tree reconstructed from archive bytes.
Archive members are streamed or read as bytes; links are never followed/extracted.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import zipfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "brain/tools"))
import authority_contracts as contracts
import source_plan_contracts
sys.path.append(str(ROOT / "brain"))
import stage_io

PLAN_SCHEMA = "wikilean.mathlib-source-acquisition-plan/v1"
CAPTURE_SCHEMA = "wikilean.mathlib-source-capture/v1"
PROFILE_SCHEMA = "wikilean.mathlib-source-profiles/v1"
EXPORT_SCHEMA = "wikilean.mathlib-source-export/v1"
NORMALIZATION = "wikilean.mathlib-official-source-normalization/v1"
REGISTRY = ROOT / "brain/mathlib_source_profiles.json"
TOOL_FILES = (
    "brain/acquire_mathlib_sources.py", "brain/export_mathlib_sources.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py",
    "brain/tools/authority_contracts.py", "brain/tools/execution_environment.py",
    "brain/tools/source_plan_contracts.py",
)
DOCS_REPO = "leanprover-community/mathlib4_docs"
MATHLIB_REPO = "leanprover-community/mathlib4"
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILE = 512 * 1024 * 1024
MAX_TAR = 4 * 1024 * 1024 * 1024
ORACLE_MEMBER = "declarations/declaration-data.bmp"
HTML_MEMBER = "Mathlib/Topology/Basic.html"
PHYSICAL_ROOT = "mathlib_export"
REVIEWED_APACHE_LICENSE_DIGESTS = frozenset({
    "b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1",
})
# Frozen retention compatibility: later v2-only producers must not gain a
# downgrade path just because their core appears in the reviewed registry.
LEGACY_NORMALIZER_CORE_DIGESTS = frozenset({
    "36f285498225df146b7a4553783fb2783c262dc6ca6e92ab4ebcc8748a8dd724",
})


class EvidenceError(RuntimeError):
    pass


def validate_module_origins():
    reviewed = ((contracts, "brain/tools/authority_contracts.py"),
                (contracts.execution_environment_contract, "brain/tools/execution_environment.py"),
                (source_plan_contracts, "brain/tools/source_plan_contracts.py"),
                (stage_io, "brain/stage_io.py"))
    for module, relative in reviewed:
        origin = getattr(module, "__file__", None)
        if origin is None or Path(origin).resolve(strict=True) != ROOT / relative:
            raise EvidenceError(f"local helper module origin differs: {relative}")
    if source_plan_contracts.contracts is not contracts:
        raise EvidenceError("source-plan helper uses a different authority module")


validate_module_origins()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return contracts.canonical_json_bytes(value)


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EvidenceError(f"{label}: unexpected fields")
    return value


def parse(raw, label, *, artifact=False):
    parser = contracts.parse_artifact_json_bytes if artifact else contracts.parse_json_bytes
    return parser(raw, location=label)


def read_regular(path: Path, limit=MAX_FILE) -> bytes:
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in [path, *path.parents]):
        raise EvidenceError("evidence requires an absolute path with real ancestors")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise EvidenceError("evidence must be a bounded regular single-link file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > limit or signature(before) != signature(os.fstat(fd)) or signature(before) != signature(path.lstat()):
            raise EvidenceError("evidence changed during capture")
        return raw
    finally:
        os.close(fd)


def validate_plan(plan):
    exact(plan, {"schema", "run_id", "job_id", "artifact_id", "mathlib_commit", "docgen_commit"}, "plan")
    if plan["schema"] != PLAN_SCHEMA:
        raise EvidenceError("unsupported acquisition plan")
    for key in ("run_id", "job_id", "artifact_id"):
        if type(plan[key]) is not int or plan[key] <= 0:
            raise EvidenceError("GitHub IDs must be positive integers")
    for key in ("mathlib_commit", "docgen_commit"):
        if not isinstance(plan[key], str) or not SHA1.fullmatch(plan[key]):
            raise EvidenceError("revision plans require full Git commit IDs")
    return plan


def request_specs(plan):
    validate_plan(plan)
    docs = f"/repos/{DOCS_REPO}/actions"
    return [
        ("docs", "run", f"{docs}/runs/{plan['run_id']}", "application/json", 4 * 1024 * 1024),
        ("docs", "artifact", f"{docs}/artifacts/{plan['artifact_id']}", "application/json", 4 * 1024 * 1024),
        ("docs", "job", f"{docs}/jobs/{plan['job_id']}", "application/json", 4 * 1024 * 1024),
        ("docs", "job_log", f"{docs}/jobs/{plan['job_id']}/logs", "text/plain", 32 * 1024 * 1024),
        ("docs", "pages_zip", f"{docs}/artifacts/{plan['artifact_id']}/zip", "application/zip", MAX_FILE),
        ("source", "commit", f"/repos/{MATHLIB_REPO}/git/commits/{plan['mathlib_commit']}", "application/json", 4 * 1024 * 1024),
        ("source", "source_tar", f"/repos/{MATHLIB_REPO}/tarball/{plan['mathlib_commit']}", "application/gzip", MAX_FILE),
    ]


def parameters(spec):
    return {"method": "GET", "path": spec[2], "query": [],
            "headers": {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            "transport": "gh-api-hostname-github.com; existing host authentication; redirects handled by gh; no pagination/cache/retry flags"}


def request_descriptor(spec):
    return {"kind": "http_get", "uri": "https://api.github.com" + spec[2],
            "parameters_sha256": sha(canonical(parameters(spec)))}


def profile_id(profile):
    return contracts.domain_hash("wikilean.mathlib-source-tool-profile.v1", {
        key: value for key, value in profile.items() if key != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    registry = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    if canonical(registry) != raw or registry["schema"] != PROFILE_SCHEMA or not isinstance(registry["profiles"], list):
        raise EvidenceError("unsupported reviewed tool registry")
    ids = []
    for profile in registry["profiles"]:
        exact(profile, {"profile_id", "files"}, "profile")
        if not isinstance(profile["files"], list):
            raise EvidenceError("invalid reviewed closure")
        names = []
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "profile file")
            contracts.validate_literal_relative_path(item["path"], "profile file path")
            if not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
                raise EvidenceError("invalid reviewed helper digest")
            names.append(item["path"])
        if names != sorted(set(names)) or "brain/mathlib_source_evidence.py" not in names:
            raise EvidenceError("invalid reviewed implementation closure")
        if profile["profile_id"] != profile_id(profile):
            raise EvidenceError("invalid reviewed profile identity")
        ids.append(profile["profile_id"])
    if ids != sorted(set(ids)) or registry["current_profile"] not in ids:
        raise EvidenceError("invalid current profile")
    return registry


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "gh"}, "tool")
    if tool["schema"] != "wikilean.mathlib-source-acquirer-tool/v1":
        raise EvidenceError("unsupported acquisition tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    if profile is None or tool["files"] != profile["files"]:
        raise EvidenceError("unreviewed acquisition implementation generation")
    for name in ("python", "gh"):
        exact(tool[name], {"sha256", "version"}, name)
        if not isinstance(tool[name]["sha256"], str) or not SHA256.fullmatch(tool[name]["sha256"]):
            raise EvidenceError("invalid executable identity")
    if not re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]):
        raise EvidenceError("isolated CPython3.12 is required")
    if not isinstance(tool["gh"]["version"], str) or not tool["gh"]["version"].startswith("gh version "):
        raise EvidenceError("invalid GitHub CLI identity")
    return profile


def current_profile():
    validate_module_origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    actual = [{"path": name, "sha256": sha(read_regular(ROOT / name))} for name in TOOL_FILES]
    if profile["files"] != actual:
        raise EvidenceError("current producer/exporter bytes differ from the approved whole generation")
    return profile


def normalization_implementation(normalizer=None, profile=None):
    """Bind new normalization to a sealed whole-generation preimage.

    Legacy version1 exports remain independently replayable for retention. New
    producers always emit version2; its digest covers every reviewed helper.
    Local tool metadata is receipt-role support, never claimed as upstream data.
    """
    registry = profiles()
    if normalizer is None:
        profile = current_profile()
        normalizer = {"name": "wikilean-official-mathlib-normalizer", "version": "2",
                      "sha256": sha(canonical(profile))}
    exact(normalizer, {"name", "version", "sha256"}, "normalizer")
    if normalizer["name"] != "wikilean-official-mathlib-normalizer":
        raise EvidenceError("unreviewed normalization implementation")
    if normalizer["version"] == "1" and profile is None:
        legacy = {item["sha256"] for p in registry["profiles"] for item in p["files"]
                  if item["path"] == "brain/mathlib_source_evidence.py"}
        if normalizer["sha256"] in legacy & LEGACY_NORMALIZER_CORE_DIGESTS:
            return normalizer, None
    if normalizer["version"] == "2" and profile in registry["profiles"] \
            and normalizer["sha256"] == sha(canonical(profile)):
        return normalizer, profile
    raise EvidenceError("unreviewed normalization implementation generation")


def ref(name, raw, media):
    return {"object": name, "sha256": sha(raw), "bytes": len(raw), "media_type": media}


def receipt(plan, raw_files, tool, source, when):
    validate_tool(tool)
    specs = [spec for spec in request_specs(plan) if spec[0] == source]
    requests = sorted((request_descriptor(spec) for spec in specs), key=canonical)
    pin = {"type": "git_commit", "value": plan["mathlib_commit"]} if source == "source" else {
        "type": "dataset_revision", "value": "github-actions-artifact:" + str(plan["artifact_id"])}
    result = {
        "schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "acquisition_receipt_id": "sha256:" + "0" * 64,
        "source": "mathlib-" + source, "upstream_uri": "https://github.com/" + (MATHLIB_REPO if source == "source" else DOCS_REPO),
        "pin": pin, "tool": {"name": "wikilean-official-mathlib-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "batch": {"status": "complete", "request_set_root": contracts.acquisition_request_set_root(requests),
            "requests_total": len(specs), "requests_succeeded": len(specs), "requests_failed": 0},
        "outputs": sorted((ref(spec[1], raw_files[spec[1]], spec[3]) for spec in specs), key=lambda x: x["object"]),
        "audit": {"acquired_at": when},
    }
    result["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(result)
    contracts.validate_acquisition_receipt(result)
    return result


def _safe_member(name):
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return ""
    contracts.validate_literal_relative_path(name.rstrip("/"), "archive member")
    return name.rstrip("/")


def extract_docs(raw):
    selected = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != "artifact.tar" or entries[0].file_size > MAX_TAR \
                or entries[0].flag_bits & 1:
            raise EvidenceError("docs ZIP must contain exactly one bounded unencrypted artifact.tar")
        seen = set()
        with archive.open(entries[0]) as stream, tarfile.open(fileobj=stream, mode="r|") as members:
            for member in members:
                name = _safe_member(member.name)
                if not name:
                    continue
                if name in seen:
                    raise EvidenceError("duplicate docs archive member")
                seen.add(name)
                if name not in {ORACLE_MEMBER, HTML_MEMBER}:
                    continue
                if not member.isfile() or member.size > MAX_FILE:
                    raise EvidenceError("selected docs member must be a bounded regular file")
                selected[name] = members.extractfile(member).read()
    if set(selected) != {ORACLE_MEMBER, HTML_MEMBER}:
        raise EvidenceError("docs artifact is missing the oracle or source-link witness")
    oracle = parse(selected[ORACLE_MEMBER], "declaration oracle", artifact=True)
    if not isinstance(oracle, dict) or not isinstance(oracle.get("declarations"), dict) or not oracle["declarations"]:
        raise EvidenceError("declaration oracle has no declarations")
    return selected


def git_hash(kind, raw):
    return hashlib.sha1(kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def tree_hash(files):
    tree = {}
    for path, (mode, raw) in files.items():
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            value = node.setdefault(part, {})
            if not isinstance(value, dict):
                raise EvidenceError("Git tree file/directory collision")
            node = value
        if parts[-1] in node:
            raise EvidenceError("duplicate Git tree path")
        node[parts[-1]] = (mode, git_hash("blob", raw))
    def encode(node):
        rows = []
        for name, value in node.items():
            mode, oid = ("40000", encode(value)) if isinstance(value, dict) else value
            key = name.encode("utf-8") + (b"/" if mode == "40000" else b"")
            rows.append((key, mode.encode() + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(oid)))
        return git_hash("tree", b"".join(row for _, row in sorted(rows)))
    return encode(tree)


def extract_source(raw, expected_tree):
    files = {}
    prefix = None
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as archive:
        for member in archive:
            name = _safe_member(member.name)
            if not name:
                continue
            parts = name.split("/", 1)
            if prefix is None:
                prefix = parts[0]
            if parts[0] != prefix:
                raise EvidenceError("source archive has multiple top-level roots")
            if len(parts) == 1:
                if not member.isdir():
                    raise EvidenceError("source archive root must be a directory")
                continue
            path = parts[1]
            if member.isdir():
                continue
            if path in files:
                raise EvidenceError("duplicate source archive member")
            if member.issym():
                mode, data = "120000", member.linkname.encode("utf-8")
            elif member.isfile() and member.size <= MAX_FILE:
                mode = "100755" if member.mode & 0o111 else "100644"
                data = archive.extractfile(member).read()
            else:
                raise EvidenceError("source archive contains an unsupported entry")
            total += len(data)
            if total > MAX_TAR or len(files) > 100_000:
                raise EvidenceError("source archive exceeds reviewed resource limits")
            files[path] = (mode, data)
    if not files or tree_hash(files) != expected_tree:
        raise EvidenceError("source archive does not reconstruct the official Git root tree")
    return files


def normalize(plan, raw_files):
    specs = request_specs(plan)
    if set(raw_files) != {spec[1] for spec in specs}:
        raise EvidenceError("acquisition must contain the exact complete request set")
    run = parse(raw_files["run"], "run", artifact=True)
    artifact = parse(raw_files["artifact"], "artifact", artifact=True)
    job = parse(raw_files["job"], "job", artifact=True)
    commit = parse(raw_files["commit"], "commit", artifact=True)
    if run.get("id") != plan["run_id"] or run.get("repository", {}).get("full_name") != DOCS_REPO \
            or run.get("status") != "completed" or run.get("conclusion") != "success":
        raise EvidenceError("official docs run identity or successful completion differs")
    if artifact.get("id") != plan["artifact_id"] or artifact.get("name") != "github-pages" \
            or artifact.get("workflow_run", {}).get("id") != plan["run_id"] \
            or artifact["workflow_run"].get("head_sha") != run.get("head_sha") \
            or artifact.get("digest") != "sha256:" + sha(raw_files["pages_zip"]) \
            or artifact.get("size_in_bytes") != len(raw_files["pages_zip"]):
        raise EvidenceError("official artifact identity/digest/size/run association differs")
    if job.get("id") != plan["job_id"] or job.get("run_id") != plan["run_id"] \
            or job.get("head_sha") != run.get("head_sha") or job.get("status") != "completed" \
            or job.get("conclusion") != "success":
        raise EvidenceError("official job is not the completed job for this docs run")
    if commit.get("sha") != plan["mathlib_commit"] or not SHA1.fullmatch(commit.get("tree", {}).get("sha", "")):
        raise EvidenceError("official Mathlib commit/tree identity differs")
    log = raw_files["job_log"].decode("utf-8")
    # Require the actual checkout log command/result pair, after its upstream
    # fetch announcement, not an arbitrary occurrence in later compiler text.
    fetch = re.search(r"From https://github\.com/leanprover-community/mathlib4(?:\.git)?\r?\n", log)
    pair = re.search(r"\[command\]/usr/bin/git log -1 --format=%H\r?\n[^\r\n]*? ([0-9a-f]{40})\r?\n", log[fetch.end():]) if fetch else None
    if pair is None or pair[1] != plan["mathlib_commit"]:
        raise EvidenceError("job checkout log does not establish the planned Mathlib revision")
    if not re.search(r"doc-gen4[^\r\n]*" + plan["docgen_commit"], log):
        raise EvidenceError("job log does not establish the planned doc-gen revision")
    docs = extract_docs(raw_files["pages_zip"])
    html = docs[HTML_MEMBER].decode("utf-8")
    links = re.findall(r"https://github\.com/leanprover-community/mathlib4/(?:blob|tree)/([0-9a-f]{40})/Mathlib/Topology/Basic\.lean", html)
    if not links or set(links) != {plan["mathlib_commit"]}:
        raise EvidenceError("rendered documentation source links disagree with the Mathlib revision")
    source = extract_source(raw_files["source_tar"], commit["tree"]["sha"])
    if "LICENSE" not in source or "Mathlib/Topology/Basic.lean" not in source:
        raise EvidenceError("source tree lacks the license or source-link witness")
    if source["LICENSE"][0] != "100644" or sha(source["LICENSE"][1]) not in REVIEWED_APACHE_LICENSE_DIGESTS:
        raise EvidenceError("source LICENSE bytes do not match the reviewed Apache-2.0 policy")
    facts = {"schema": NORMALIZATION, "mathlib_commit": plan["mathlib_commit"],
        "mathlib_tree": commit["tree"]["sha"], "docs_commit": run["head_sha"],
        "docgen_commit": plan["docgen_commit"], "run_id": plan["run_id"],
        "job_id": plan["job_id"], "artifact_id": plan["artifact_id"],
        "pages_zip_sha256": sha(raw_files["pages_zip"]), "oracle_sha256": sha(docs[ORACLE_MEMBER]),
        "oracle_members": len(parse(docs[ORACLE_MEMBER], "oracle", artifact=True)["declarations"]),
        "source_members": len(source), "source_license_sha256": sha(source["LICENSE"][1]),
        "review_state": "verified-upstream-content; source-plan-review-required; private-export"}
    return docs, source, facts


def manifest_files(files, schema):
    document = {"schema": schema, "files": [{"path": name, "sha256": sha(raw), "bytes": len(raw)}
                                             for name, raw in sorted(files.items())]}
    document["identity"] = contracts.domain_hash(schema, document)
    return {**files, "manifest.json": canonical(document)}


def read_bundle(path, schema):
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise EvidenceError("bundle root must be a current-user-owned 0700 directory")
    raw = read_regular(path / "manifest.json", 8 * 1024 * 1024)
    manifest = exact(parse(raw, "bundle manifest"), {"schema", "files", "identity"}, "bundle manifest")
    if canonical(manifest) != raw or manifest["schema"] != schema or not isinstance(manifest["files"], list):
        raise EvidenceError("unsupported bundle manifest")
    identity = contracts.domain_hash(schema, {k: v for k, v in manifest.items() if k != "identity"})
    if manifest["identity"] != identity:
        raise EvidenceError("bundle identity differs")
    files = {}
    for item in manifest["files"]:
        exact(item, {"path", "sha256", "bytes"}, "bundle file")
        contracts.validate_literal_relative_path(item["path"], "bundle file")
        if item["path"] in files or item["path"] == "manifest.json":
            raise EvidenceError("duplicate bundle file")
        data = read_regular(path / item["path"])
        if sha(data) != item["sha256"] or len(data) != item["bytes"]:
            raise EvidenceError("bundle member content differs")
        files[item["path"]] = data
    actual, actual_directories = set(), set()
    for directory, names, filenames in os.walk(path):
        for name in names:
            child = Path(directory) / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise EvidenceError("bundle contains an unsafe directory")
            actual_directories.add(child.relative_to(path).as_posix())
        for name in filenames:
            child = Path(directory) / name
            info = child.lstat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o644:
                raise EvidenceError("bundle contains an unsafe member")
            actual.add(child.relative_to(path).as_posix())
    if actual != {*files, "manifest.json"}:
        raise EvidenceError("bundle contains undeclared files")
    expected_directories = {parent.as_posix() for name in files for parent in Path(name).parents if parent != Path(".")}
    if actual_directories != expected_directories:
        raise EvidenceError("bundle contains undeclared directories")
    return files, manifest


def publish(files, store, schema):
    if not store.is_absolute() or ".." in store.parts:
        raise EvidenceError("output store must be an absolute literal path")
    anchor = store
    while not anchor.exists() and not anchor.is_symlink():
        anchor = anchor.parent
    stage_io.ensure_private_directory(anchor, store)
    info = store.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or store.is_symlink():
        raise EvidenceError("output store must be private and current-user-owned")
    document = parse(files["manifest.json"], "manifest")
    target = store / document["identity"].removeprefix("sha256:")
    with stage_io.owned_directory(store, store / (".mathlib-" + uuid.uuid4().hex)) as owned:
        for name, raw in sorted(files.items()):
            stage_io.ensure_private_directory(owned.path, (owned.path / name).parent)
            stage_io.write_bytes_exclusive(owned.path / name, raw, mode=0o644)
        for directory, _names, _files in os.walk(owned.path, topdown=False):
            stage_io.fsync_directory(Path(directory))
        if read_bundle(owned.path, schema)[0] != {k: v for k, v in files.items() if k != "manifest.json"}:
            raise EvidenceError("staged bundle readback differs")
        try:
            stage_io.publish_directory_no_replace(owned, target)
        except FileExistsError:
            if read_bundle(target, schema)[0] != {k: v for k, v in files.items() if k != "manifest.json"}:
                raise EvidenceError("existing immutable generation differs")
    stage_io.fsync_directory(store)
    read_bundle(target, schema)
    return target


def capture_files(plan, raw_files, tool, when):
    validate_plan(plan)
    validate_tool(tool)
    specs = request_specs(plan)
    if set(raw_files) != {s[1] for s in specs}:
        raise EvidenceError("incomplete fresh acquisition")
    files = {"plan.json": canonical(plan), "tool.json": canonical(tool)}
    records = []
    for spec in specs:
        name, data = spec[1], raw_files[spec[1]]
        if len(data) > spec[4]:
            raise EvidenceError("acquired response exceeds bound")
        files["raw/" + name] = data
        files["requests/" + name + ".json"] = canonical(parameters(spec))
        records.append({"object": name, "request": request_descriptor(spec), "result": "gh-api-exit-zero",
                        "exit_code": 0, "sha256": sha(data), "bytes": len(data)})
    files["request-results.json"] = canonical(records)
    for source in ("docs", "source"):
        files[f"receipts/{source}.json"] = canonical(receipt(plan, raw_files, tool, source, when))
    return manifest_files(files, CAPTURE_SCHEMA)


def verify_capture(path):
    files, manifest = read_bundle(path, CAPTURE_SCHEMA)
    plan = validate_plan(parse(files["plan.json"], "plan"))
    tool = parse(files["tool.json"], "tool")
    validate_tool(tool)
    raw_files = {spec[1]: files["raw/" + spec[1]] for spec in request_specs(plan)}
    docs_receipt = parse(files["receipts/docs.json"], "receipt")
    expected = capture_files(plan, raw_files, tool, docs_receipt["audit"]["acquired_at"])
    if expected != {**files, "manifest.json": canonical(manifest)}:
        raise EvidenceError("capture receipt/request/output closure differs")
    return plan, raw_files, tool, files


def build_export(plan, raw_files, tool, capture, when, *, normalizer=None, normalization_profile=None):
    docs, source_files, facts = normalize(plan, raw_files)
    validate_tool(tool)
    normalizer, normalization_profile = normalization_implementation(normalizer, normalization_profile)
    files = {"acquisition/" + name: raw for name, raw in capture.items()}
    files["normalization/plan.json"] = canonical(plan)
    files["normalization/facts.json"] = canonical(facts)
    if normalization_profile is not None:
        files["normalization/tool-profile.json"] = canonical(normalization_profile)
    objects = {}
    def planned(name, raw, roles, media):
        path = "objects/sha256/" + sha(raw)
        files.setdefault(path, raw)
        return {"name": name, "root": PHYSICAL_ROOT, "path": path, "roles": sorted(roles),
                "sha256": sha(raw), "bytes": len(raw), "media_type": media, "redistribution": "restricted"}
    def evidence(path, identity):
        return {"root": PHYSICAL_ROOT, "path": path, "sha256": sha(files[path]),
                "bytes": len(files[path]), "media_type": "application/json", **identity}
    def projected(item):
        return {"object": item["name"], **{k: item[k] for k in ("sha256", "bytes", "media_type")}}
    tree = {"commit": plan["mathlib_commit"], "tree": facts["mathlib_tree"],
            "entries": [{"path": path, "mode": mode, "git_blob": git_hash("blob", raw),
                         "sha256": sha(raw), "bytes": len(raw)} for path, (mode, raw) in sorted(source_files.items())]}
    source_outputs = [planned("git_tree", canonical(tree), ["normalized"], "application/json")]
    source_members = []
    for path, (mode, raw) in sorted(source_files.items()):
        name = "file-" + sha(path.encode())
        item = planned(name, raw, ["normalized"], "application/octet-stream")
        source_outputs.append(item)
        if path.startswith("Mathlib/") and path.endswith(".lean"):
            if mode not in {"100644", "100755"}:
                raise EvidenceError("Mathlib source binding cannot follow a symbolic link")
            source_members.append({"path": path, "source": "mathlib-source", "object": name})
    docs_outputs = [planned("declaration_oracle", docs[ORACLE_MEMBER], ["normalized"], "application/json"),
                    planned("source_html", docs[HTML_MEMBER], ["normalized"], "text/html"),
                    planned("provenance", canonical(facts), ["normalized"], "application/json")]
    sources, manifests = [], {}
    for kind, outputs in (("source", source_outputs), ("docs", docs_outputs)):
        original = parse(capture[f"receipts/{kind}.json"], "receipt")
        raw = [planned(spec[1], raw_files[spec[1]], ["raw"], spec[3]) for spec in request_specs(plan) if spec[0] == kind]
        inputs = [{**projected(item), "origin": {"kind": "acquisition_receipt", "id": original["acquisition_receipt_id"]}} for item in raw]
        parents = {}
        if kind == "docs":
            parent = manifests["mathlib-source"]
            parent_tree = source_outputs[0]
            raw.append({**parent_tree, "roles": ["raw"]})
            inputs.append({**projected(parent_tree), "origin": {"kind": "source_manifest", "id": parent["source_manifest_id"]}})
            parents[parent["source_manifest_id"]] = parent
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": "mathlib-" + kind, "mode": "transform", "acquisition_receipt_ids": [original["acquisition_receipt_id"]],
            "parent_source_manifest_ids": sorted(parents), "normalization_schema": NORMALIZATION + "/" + kind,
            "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
            "inputs": sorted(inputs, key=lambda x: (x["origin"]["kind"], x["origin"]["id"], x["object"])),
            "outputs": sorted(map(projected, outputs), key=lambda x: x["object"]),
            "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        contracts.validate_normalization_lineage(lineage)
        lineage_path = f"evidence/{kind}-lineage.json"
        files[lineage_path] = canonical(lineage)
        support = [planned("normalization_plan", canonical(plan), ["receipt"], "application/json"),
                   planned("acquisition_tool", canonical(tool), ["receipt"], "application/json")]
        if normalization_profile is not None:
            support.append(planned("normalization_tool_profile", canonical(normalization_profile), ["receipt"], "application/json"))
        prospective = {"source": "mathlib-" + kind, "source_kind": "acquired_dataset", "pin": original["pin"],
            "objects": sorted([*raw, *outputs, *support], key=lambda x: x["name"]),
            "license": {"expression": "Apache-2.0" if kind == "source" else "LicenseRef-Mathlib-Docs-Review", "redistribution": "restricted",
                "notice": "Private evidence export; upstream LICENSE is sealed in the source tree; source-plan and redistribution review remain required."},
            "acquisition": original["tool"], "normalization": {"schema": lineage["normalization_schema"], "tool": normalizer,
                "inputs": sorted(item["name"] for item in raw), "outputs": sorted(item["name"] for item in outputs)},
            "evidence": {"acquisition_receipts": [evidence(f"acquisition/receipts/{kind}.json", {"acquisition_receipt_id": original["acquisition_receipt_id"]})],
                "normalization_lineage": evidence(lineage_path, {"normalization_lineage_id": lineage["normalization_lineage_id"]}),
                "request_parameter_preimages": sorted([evidence("acquisition/requests/" + spec[1] + ".json", {
                    "parameters_sha256": sha(canonical(parameters(spec)))}) for spec in request_specs(plan) if spec[0] == kind], key=lambda x: x["parameters_sha256"])},
            "audit": {"acquired_at": original["audit"]["acquired_at"], "upstream_uri": original["upstream_uri"]}}
        manifest = source_plan_contracts._source_manifest_from_plan(prospective, "mathlib source")
        contracts.validate_source_manifest_evidence_documents(manifest,
            receipts={original["acquisition_receipt_id"]: original}, lineage=lineage,
            request_parameter_preimages={x["parameters_sha256"]: {k: x[k] for k in ("parameters_sha256", "bytes", "media_type")}
                for x in prospective["evidence"]["request_parameter_preimages"]}, parent_source_manifests=parents)
        manifests[prospective["source"]] = manifest
        sources.append(prospective)
        files[f"source-manifests/{kind}.json"] = canonical(manifest)
    fragment = {"schema": "wikilean.mathlib-source-plan-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": PHYSICAL_ROOT, "source_publishable": False, "redistribution": "restricted",
        "review_state": facts["review_state"], "sources": sorted(sources, key=lambda x: x["source"]),
        "input_bindings": [
            {"input_id": "declaration-oracle", "state": "present", "sources": ["mathlib-docs"], "members": [
                {"path": "declaration-data.json", "source": "mathlib-docs", "object": "declaration_oracle"}]},
            {"input_id": "mathlib-source-tree", "state": "present", "sources": ["mathlib-source"], "members": source_members}]}
    files["source-fragment.json"] = canonical(fragment)
    return manifest_files(files, EXPORT_SCHEMA)
