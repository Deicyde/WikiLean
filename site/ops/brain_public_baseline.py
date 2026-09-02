#!/usr/bin/env python3
"""Freeze and verify immutable non-Brain Worker public assets.

A public-asset baseline is a content-addressed directory whose payload is every
regular file in a Worker public tree except the release-coupled Brain surface
(``brain.html`` and ``assets/brain/**``).  It is intended to be combined with
one separately verified Brain release by the exact-release promoter.

The baseline identity commits to the authority Git commit, every payload path,
byte length, and SHA-256 digest.  Freezing succeeds only when those bytes match
the canonical source inventory stored at ``wiki/public-asset-source-attestation.json``
in that exact Git commit; an ignored or dirty ``wiki/public`` tree cannot claim
an unrelated authority.  Publication is crash-safe: files are copied into a
private pending sibling, synced, made read-only, and atomically renamed to
``<baseline-store>/<baseline-hex>``.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence


BASELINE_SCHEMA = "wikilean.public-asset-baseline/v1"
BASELINE_DOMAIN = "wikilean.public-asset-baseline.v1"
SOURCE_ATTESTATION_SCHEMA = "wikilean.public-asset-source-attestation/v1"
SOURCE_ATTESTATION_PATH = "wiki/public-asset-source-attestation.json"
MANIFEST_NAME = "manifest.json"
BASELINE_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_INDEX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENTS = 64
COPY_BUFFER_BYTES = 1024 * 1024

CRITICAL_PATHS = frozenset(
    {
        "404.html",
        "robots.txt",
        "wikilean.ttl",
        "concepts.html",
        "assets/style.css",
        "assets/script.js",
        "assets/review.css",
        "assets/editor.js",
        "assets/mathlib-index.json",
        "assets/decl-index/manifest.json",
        "assets/suffix-index/manifest.json",
        "assets/premise-index/manifest.json",
    }
)
INDEX_FAMILIES = (
    "assets/decl-index/",
    "assets/suffix-index/",
    "assets/premise-index/",
)
INDEX_FAMILY_NAMES = ("decl-index", "suffix-index", "premise-index")
INDEX_SHARD_KEY_RE = re.compile(r"^[a-z0-9_]{2,64}$")
FORBIDDEN_PATHS = frozenset(
    {
        "index.html",
        "sitemap.xml",
        "about.html",
        "map.html",
        "map-v2.html",
        "graph.html",
        "graph_data.json",
        "atlas.html",
        "atlas_data.json",
        "article-graph.html",
        "article-graph-data.json",
        "map_data.json",
        "map_data_v2.json",
    }
)
ALLOWED_TOP_LEVEL_FILES = frozenset({"404.html", "robots.txt", "wikilean.ttl", "concepts.html"})

_MANIFEST_KEYS = frozenset({"schema", "baseline_id", "authority", "files"})
_AUTHORITY_KEYS = frozenset({"git_commit"})
_FILE_KEYS = frozenset({"path", "sha256", "bytes"})
_SOURCE_ATTESTATION_KEYS = frozenset({"schema", "files"})


class PublicBaselineError(RuntimeError):
    """Base class for public-baseline failures."""


class BaselineValidationError(PublicBaselineError):
    """A source tree or frozen baseline violates the artifact contract."""


class BaselineFreezeError(PublicBaselineError):
    """A baseline cannot be safely or durably frozen."""


@dataclass(frozen=True)
class PublicAssetFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class PublicAssetBaseline:
    root: Path
    manifest_path: Path
    baseline_id: str
    baseline_hex: str
    authority_git_commit: str
    files: tuple[PublicAssetFile, ...]
    total_bytes: int

    def summary(self) -> dict[str, object]:
        return {
            "ok": True,
            "schema": BASELINE_SCHEMA,
            "baseline_id": self.baseline_id,
            "root": str(self.root),
            "manifest": str(self.manifest_path),
            "authority_git_commit": self.authority_git_commit,
            "files": len(self.files),
            "bytes": self.total_bytes,
        }


def _canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise BaselineValidationError(f"manifest contains unsupported JSON: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def _reject_constant(value: str) -> object:
    raise BaselineValidationError(f"manifest contains non-finite number {value}")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BaselineValidationError(f"manifest contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_manifest_bytes(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BaselineValidationError("manifest exceeds the supported size limit")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BaselineValidationError("manifest is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BaselineValidationError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BaselineValidationError("manifest must be a JSON object")
    if _canonical_json_bytes(value) != raw:
        raise BaselineValidationError("manifest is not canonical JSON")
    return value


def _exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise BaselineValidationError(f"{label} fields are invalid ({'; '.join(details)})")


def _validate_relative_path(value: object, label: str = "file path") -> str:
    if not isinstance(value, str) or not value:
        raise BaselineValidationError(f"{label} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise BaselineValidationError(f"{label} must use NFC Unicode normalization")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BaselineValidationError(f"{label} is not valid UTF-8") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise BaselineValidationError(f"{label} exceeds {MAX_PATH_BYTES} UTF-8 bytes")
    if "\\" in value or "\0" in value or value.startswith("/"):
        raise BaselineValidationError(f"{label} is not a safe relative POSIX path: {value!r}")
    parts = value.split("/")
    if (
        len(parts) > MAX_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
        or posixpath.normpath(value) != value
    ):
        raise BaselineValidationError(f"{label} is not normalized: {value!r}")
    if value == MANIFEST_NAME:
        raise BaselineValidationError(f"{MANIFEST_NAME} is reserved for baseline metadata")
    return value


def _is_brain_owned(path: str) -> bool:
    return path == "brain.html" or path == "assets/brain" or path.startswith("assets/brain/")


def _is_route_shadowing_or_retired(path: str) -> bool:
    return path in FORBIDDEN_PATHS or (
        not path.startswith("assets/") and path not in ALLOWED_TOP_LEVEL_FILES
    )


def _validate_commit(value: object, label: str = "authority.git_commit") -> str:
    if not isinstance(value, str) or not GIT_COMMIT_RE.fullmatch(value):
        raise BaselineValidationError(
            f"{label} must be a full 40-character lowercase Git commit"
        )
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise BaselineValidationError(f"{label} must be 64 lowercase SHA-256 hex digits")
    return value


def _validate_bytes(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineValidationError(f"{label} must be an integer")
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise BaselineValidationError(f"{label} is outside the safe integer range")
    return value


def _identity_document(
    authority_git_commit: str,
    files: Sequence[PublicAssetFile],
) -> dict[str, object]:
    return {
        "schema": BASELINE_SCHEMA,
        "authority": {"git_commit": authority_git_commit},
        "files": [
            {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
            for item in files
        ],
    }


def _baseline_id(identity: Mapping[str, object]) -> str:
    payload = (
        b"wikilean\0"
        + BASELINE_DOMAIN.encode("ascii")
        + b"\0canonical-json-v1\0"
        + _canonical_json_bytes(identity).removesuffix(b"\n")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_required_payload(files: Sequence[PublicAssetFile]) -> None:
    by_path = {item.path: item for item in files}
    missing = sorted(CRITICAL_PATHS - by_path.keys())
    if missing:
        raise BaselineValidationError(
            "baseline is missing required public assets: " + ", ".join(missing)
        )
    empty_critical = sorted(path for path in CRITICAL_PATHS if by_path[path].bytes == 0)
    if empty_critical:
        raise BaselineValidationError(
            "required public assets must be nonempty: " + ", ".join(empty_critical)
        )
    for prefix in INDEX_FAMILIES:
        payload = [
            item
            for item in files
            if item.path.startswith(prefix) and item.path != prefix + "manifest.json"
        ]
        if not payload:
            raise BaselineValidationError(
                f"index family {prefix.rstrip('/')} must contain at least one payload file"
            )
        empty = sorted(item.path for item in payload if item.bytes == 0)
        if empty:
            raise BaselineValidationError(
                f"index family {prefix.rstrip('/')} contains empty files: "
                + ", ".join(empty)
            )


def _validate_file_inventory(
    raw_files: object,
    *,
    label: str,
) -> tuple[PublicAssetFile, ...]:
    if not isinstance(raw_files, list):
        raise BaselineValidationError(f"{label} files must be an array")
    files: list[PublicAssetFile] = []
    previous_path: str | None = None
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict):
            raise BaselineValidationError(f"{label}.files[{index}] must be an object")
        field_label = f"{label}.files[{index}]"
        _exact_keys(raw_file, _FILE_KEYS, field_label)
        path = _validate_relative_path(raw_file["path"], f"{field_label}.path")
        if _is_brain_owned(path):
            raise BaselineValidationError(f"Brain-owned path must not enter a baseline: {path}")
        if _is_route_shadowing_or_retired(path):
            raise BaselineValidationError(
                f"route-shadowing or retired path must not enter a baseline: {path}"
            )
        if previous_path is not None and path <= previous_path:
            if path == previous_path:
                raise BaselineValidationError(f"manifest contains duplicate file path {path!r}")
            raise BaselineValidationError("manifest files must be sorted by path")
        previous_path = path
        files.append(
            PublicAssetFile(
                path=path,
                sha256=_validate_digest(raw_file["sha256"], f"{field_label}.sha256"),
                bytes=_validate_bytes(raw_file["bytes"], f"{field_label}.bytes"),
            )
        )

    _validate_required_payload(files)
    return tuple(files)


def _validate_manifest(document: Mapping[str, object]) -> tuple[str, str, tuple[PublicAssetFile, ...]]:
    _exact_keys(document, _MANIFEST_KEYS, "manifest")
    if document["schema"] != BASELINE_SCHEMA:
        raise BaselineValidationError("manifest has an unknown schema/version")

    raw_authority = document["authority"]
    if not isinstance(raw_authority, dict):
        raise BaselineValidationError("manifest authority must be an object")
    _exact_keys(raw_authority, _AUTHORITY_KEYS, "authority")
    authority = _validate_commit(raw_authority["git_commit"])

    files = _validate_file_inventory(document["files"], label="manifest")
    identity = _identity_document(authority, files)
    expected_id = _baseline_id(identity)
    raw_id = document["baseline_id"]
    if not isinstance(raw_id, str) or not BASELINE_ID_RE.fullmatch(raw_id):
        raise BaselineValidationError(
            "manifest baseline_id must be sha256: followed by 64 lowercase hex digits"
        )
    if raw_id != expected_id:
        raise BaselineValidationError(
            f"manifest baseline_id mismatch: declared {raw_id}, computed {expected_id}"
        )
    return raw_id, authority, files


def _lstat_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BaselineValidationError(f"cannot inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BaselineValidationError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise BaselineValidationError(f"{label} must be a directory: {path}")
    return info


def _resolve_repo(repo_root: os.PathLike[str] | str) -> Path:
    supplied = Path(repo_root)
    if not supplied.is_absolute():
        raise BaselineValidationError("repository root must be an absolute path")
    _lstat_directory(supplied, "repository root")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise BaselineValidationError(f"cannot resolve repository root: {exc}") from exc


def _run_git(repository: Path, arguments: Sequence[str], label: str) -> bytes:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
            "GIT_REPLACE_REF_BASE",
        } or name.startswith("GIT_CONFIG_"):
            environment.pop(name, None)
    # A local replace ref can otherwise make an object named by one SHA resolve
    # to unrelated bytes.  Authority is the literal commit object supplied by
    # the caller, not a mutable per-checkout rewrite.
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineValidationError(f"cannot {label}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise BaselineValidationError(f"cannot {label}{suffix}")
    return result.stdout


def _validate_git_repository(repository: Path) -> None:
    raw_root = _run_git(repository, ["rev-parse", "--show-toplevel"], "locate repository root")
    try:
        declared_root = Path(raw_root.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise BaselineValidationError(f"Git returned an invalid repository root: {exc}") from exc
    if declared_root != repository:
        raise BaselineValidationError(
            f"repository root must be the Git worktree root: expected {declared_root}, got {repository}"
        )


def _load_source_attestation(
    repository: Path,
    authority_git_commit: str,
) -> tuple[PublicAssetFile, ...]:
    """Load the public inventory from the literal authority commit.

    The generated ``wiki/public`` tree is ignored by Git and therefore cannot
    establish its own provenance.  The authority commit must carry a canonical
    inventory at one fixed tracked path; reading that blob through Git binds
    every accepted output byte to the commit without trusting dirty worktree
    files or a caller-selected sidecar.
    """

    _validate_git_repository(repository)
    try:
        resolved = _run_git(
            repository,
            ["rev-parse", "--verify", f"{authority_git_commit}^{{commit}}"],
            "resolve authority Git commit",
        ).decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BaselineValidationError("Git returned an invalid authority commit id") from exc
    if resolved != authority_git_commit:
        raise BaselineValidationError(
            f"authority Git object did not resolve to the exact commit {authority_git_commit}"
        )

    entry = _run_git(
        repository,
        ["ls-tree", "-z", authority_git_commit, "--", SOURCE_ATTESTATION_PATH],
        "locate committed public source attestation",
    )
    records = [record for record in entry.split(b"\0") if record]
    if len(records) != 1:
        raise BaselineValidationError(
            f"authority commit must contain exactly one {SOURCE_ATTESTATION_PATH} blob"
        )
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii", errors="strict").split(" ")
        recorded_path = raw_path.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise BaselineValidationError("Git returned an invalid source attestation tree entry") from exc
    if recorded_path != SOURCE_ATTESTATION_PATH or mode != "100644" or kind != "blob":
        raise BaselineValidationError(
            f"{SOURCE_ATTESTATION_PATH} must be a non-executable regular file in the authority commit"
        )
    if not GIT_OBJECT_RE.fullmatch(object_id):
        raise BaselineValidationError("Git returned an invalid source attestation blob id")
    try:
        size = int(
            _run_git(
                repository,
                ["cat-file", "-s", object_id],
                "inspect committed public source attestation",
            ).decode("ascii", errors="strict").strip()
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise BaselineValidationError("Git returned an invalid source attestation size") from exc
    if size < 0 or size > MAX_MANIFEST_BYTES:
        raise BaselineValidationError("committed public source attestation exceeds the size limit")
    raw = _run_git(
        repository,
        ["cat-file", "blob", object_id],
        "read committed public source attestation",
    )
    if len(raw) != size:
        raise BaselineValidationError("committed public source attestation size changed while reading")
    document = _load_manifest_bytes(raw)
    _exact_keys(document, _SOURCE_ATTESTATION_KEYS, "source attestation")
    if document["schema"] != SOURCE_ATTESTATION_SCHEMA:
        raise BaselineValidationError("source attestation has an unknown schema/version")
    return _validate_file_inventory(document["files"], label="source attestation")


def _require_attested_source(
    actual: Sequence[PublicAssetFile],
    attested: Sequence[PublicAssetFile],
    authority_git_commit: str,
) -> None:
    if tuple(actual) == tuple(attested):
        return
    actual_by_path = {item.path: item for item in actual}
    attested_by_path = {item.path: item for item in attested}
    missing = sorted(attested_by_path.keys() - actual_by_path.keys())
    unexpected = sorted(actual_by_path.keys() - attested_by_path.keys())
    changed = sorted(
        path
        for path in actual_by_path.keys() & attested_by_path.keys()
        if actual_by_path[path] != attested_by_path[path]
    )
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unexpected:
        details.append("unattested " + ", ".join(unexpected))
    if changed:
        details.append("content mismatch " + ", ".join(changed))
    raise BaselineValidationError(
        "source public tree does not match the inventory committed at "
        f"{authority_git_commit} ({'; '.join(details)})"
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_external(path: Path, repository: Path, label: str) -> None:
    if path == repository or _is_relative_to(path, repository) or _is_relative_to(repository, path):
        raise BaselineValidationError(
            f"{label} must be outside and must not contain the repository checkout: {path}"
        )


def _resolve_source_root(source_root: os.PathLike[str] | str) -> Path:
    supplied = Path(source_root)
    if not supplied.is_absolute():
        raise BaselineValidationError("source public root must be an absolute path")
    _lstat_directory(supplied, "source public root")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise BaselineValidationError(f"cannot resolve source public root: {exc}") from exc


def _prospective_store(
    store: os.PathLike[str] | str,
    repository: Path,
) -> Path:
    supplied = Path(store)
    if not supplied.is_absolute():
        raise BaselineValidationError("baseline store must be an absolute path")
    if supplied.is_symlink():
        raise BaselineValidationError(f"baseline store must not be a symlink: {supplied}")
    try:
        physical_parent = supplied.parent.resolve(strict=True)
    except OSError as exc:
        raise BaselineValidationError(
            f"baseline store parent must already exist: {supplied.parent}"
        ) from exc
    candidate = physical_parent / supplied.name
    _require_external(candidate.resolve(strict=False), repository, "baseline store")
    return candidate


def _prepare_store(
    store: os.PathLike[str] | str,
    repository: Path,
) -> Path:
    candidate = _prospective_store(store, repository)
    physical_parent = candidate.parent
    if not candidate.exists():
        try:
            candidate.mkdir(mode=0o700)
            _fsync_directory(physical_parent)
        except OSError as exc:
            raise BaselineFreezeError(f"cannot create baseline store {candidate}: {exc}") from exc
    info = _lstat_directory(candidate, "baseline store")
    if info.st_uid != os.geteuid():
        raise BaselineValidationError(
            f"baseline store must be owned by uid {os.geteuid()}, got {info.st_uid}"
        )
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise BaselineValidationError("baseline store must not be group- or world-writable")
    if not os.access(candidate, os.R_OK | os.W_OK | os.X_OK, effective_ids=True):
        raise BaselineValidationError("baseline store must be readable and writable by its owner")
    physical = candidate.resolve(strict=True)
    _require_external(physical, repository, "baseline store")
    return physical


def _resolve_baseline_root(
    baseline_root: os.PathLike[str] | str,
    repository: Path,
) -> Path:
    supplied = Path(baseline_root)
    if not supplied.is_absolute():
        raise BaselineValidationError("baseline root must be an absolute path")
    _lstat_directory(supplied, "baseline root")
    try:
        physical = supplied.resolve(strict=True)
    except OSError as exc:
        raise BaselineValidationError(f"cannot resolve baseline root: {exc}") from exc
    _require_external(physical, repository, "baseline root")
    if not re.fullmatch(r"[0-9a-f]{64}", physical.name):
        raise BaselineValidationError(
            "baseline root basename must be 64 lowercase SHA-256 hex digits"
        )
    return physical


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync is not None:
        fcntl.fcntl(descriptor, full_fsync)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _store_lock(store: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(store / ".freeze.lock", flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise BaselineFreezeError("baseline store lock is not a regular owner-owned file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise BaselineFreezeError("baseline store lock permissions must be 0600")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _entry_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _list_directory(directory_fd: int, label: str) -> list[tuple[str, os.stat_result]]:
    result: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                name = entry.name
                if unicodedata.normalize("NFC", name) != name:
                    raise BaselineValidationError(
                        f"source entry name must use NFC Unicode normalization: {label}/{name}"
                    )
                try:
                    name.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise BaselineValidationError(
                        f"source entry name is not valid UTF-8: {label}/{name}"
                    ) from exc
                result.append((name, entry.stat(follow_symlinks=False)))
    except OSError as exc:
        raise BaselineValidationError(f"cannot scan source directory {label}: {exc}") from exc
    result.sort(key=lambda item: item[0])
    return result


def _same_listing(
    first: Sequence[tuple[str, os.stat_result]],
    second: Sequence[tuple[str, os.stat_result]],
) -> bool:
    return [(name, _entry_signature(info)) for name, info in first] == [
        (name, _entry_signature(info)) for name, info in second
    ]


def _read_open_source(
    source_fd: int,
    source_before: os.stat_result,
    relative: str,
    destination: Path | None,
) -> PublicAssetFile:
    digest = hashlib.sha256()
    size = 0
    target_fd: int | None = None
    try:
        if destination is not None:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            target_fd = os.open(destination, flags, 0o600)
        while True:
            chunk = os.read(source_fd, COPY_BUFFER_BYTES)
            if not chunk:
                break
            if target_fd is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise BaselineFreezeError(f"short write while freezing {relative}")
                    view = view[written:]
            digest.update(chunk)
            size += len(chunk)
        # The private pending tree is not publishable yet.  Its files are
        # durably synced together by _seal_pending_tree only after provenance
        # and index-closure validation succeeds.
        source_after = os.fstat(source_fd)
    except Exception:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if target_fd is not None:
            os.close(target_fd)
    if _entry_signature(source_before) != _entry_signature(source_after) or size != source_before.st_size:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise BaselineFreezeError(f"source changed while freezing: {relative}")
    return PublicAssetFile(relative, digest.hexdigest(), size)


def _copy_open_source(
    source_fd: int,
    source_before: os.stat_result,
    destination: Path,
    relative: str,
) -> PublicAssetFile:
    return _read_open_source(source_fd, source_before, relative, destination)


def _read_source_tree(
    source: Path,
    destination: Path | None,
) -> tuple[PublicAssetFile, ...]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(source, directory_flags)
    files: list[PublicAssetFile] = []

    def walk(directory_fd: int, prefix: str) -> None:
        before = _list_directory(directory_fd, prefix or ".")
        for name, discovered in before:
            relative = f"{prefix}/{name}" if prefix else name
            # Validate names before considering the Brain exclusion so unsafe
            # structure cannot hide below a skipped namespace.
            if len(relative.encode("utf-8")) > MAX_PATH_BYTES or len(relative.split("/")) > MAX_PATH_COMPONENTS:
                raise BaselineValidationError(f"source path exceeds supported limits: {relative!r}")
            mode = discovered.st_mode
            if stat.S_ISLNK(mode):
                raise BaselineValidationError(f"source contains a symlink: {relative}")
            if stat.S_ISDIR(mode):
                if relative == MANIFEST_NAME or relative in CRITICAL_PATHS or relative == "brain.html":
                    raise BaselineValidationError(
                        f"source path reserved for a regular file is a directory: {relative}"
                    )
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if opened.st_dev != discovered.st_dev or opened.st_ino != discovered.st_ino:
                        raise BaselineFreezeError(f"source directory changed while opening: {relative}")
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(mode):
                raise BaselineValidationError(f"source contains a non-regular file: {relative}")
            _validate_relative_path(relative, "source path")
            if _is_brain_owned(relative):
                continue
            if _is_route_shadowing_or_retired(relative):
                raise BaselineValidationError(
                    f"route-shadowing or retired path must not enter a baseline: {relative}"
                )
            source_fd = os.open(name, file_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(source_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise BaselineValidationError(f"source is not a regular file: {relative}")
                if opened.st_dev != discovered.st_dev or opened.st_ino != discovered.st_ino:
                    raise BaselineFreezeError(f"source file changed while opening: {relative}")
                target = (
                    destination / PurePosixPath(relative)
                    if destination is not None
                    else None
                )
                files.append(_read_open_source(source_fd, opened, relative, target))
            finally:
                os.close(source_fd)
        after = _list_directory(directory_fd, prefix or ".")
        if not _same_listing(before, after):
            raise BaselineFreezeError(f"source directory changed while freezing: {prefix or '.'}")

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    files.sort(key=lambda item: item.path)
    return tuple(files)


def _copy_source_tree(source: Path, pending: Path) -> tuple[PublicAssetFile, ...]:
    return _read_source_tree(source, pending)


def _inventory_source_tree(source: Path) -> tuple[PublicAssetFile, ...]:
    return _read_source_tree(source, None)


def _write_manifest(path: Path, document: Mapping[str, object]) -> None:
    raw = _canonical_json_bytes(document)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise BaselineFreezeError("short write while freezing manifest")
            view = view[written:]
        _fsync_file(descriptor)
    finally:
        os.close(descriptor)


def _seal_pending_tree(root: Path) -> None:
    files: list[Path] = []
    directories: list[Path] = [root]
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in dirnames:
            child = current_path / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BaselineFreezeError(f"pending baseline contains unsafe directory: {child}")
            directories.append(child)
        for name in filenames:
            child = current_path / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BaselineFreezeError(f"pending baseline contains unsafe file: {child}")
            files.append(child)
    for path in files:
        path.chmod(0o444)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _fsync_file(descriptor)
        finally:
            os.close(descriptor)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)
        _fsync_directory(path)


def _remove_pending(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    for current, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        with contextlib.suppress(OSError):
            current_path.chmod(0o700)
        for name in filenames:
            child = current_path / name
            with contextlib.suppress(OSError):
                child.chmod(0o600)
            with contextlib.suppress(OSError):
                child.unlink()
        for name in dirnames:
            child = current_path / name
            if child.is_symlink():
                with contextlib.suppress(OSError):
                    child.unlink()
            else:
                with contextlib.suppress(OSError):
                    child.chmod(0o700)
                with contextlib.suppress(OSError):
                    child.rmdir()
    with contextlib.suppress(OSError):
        path.rmdir()


def _open_baseline_file(root: Path, relative: str) -> tuple[int, os.stat_result]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in PurePosixPath(relative).parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(PurePosixPath(relative).name, file_flags, dir_fd=descriptors[-1])
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            os.close(descriptor)
            raise BaselineValidationError(f"baseline entry is not a regular file: {relative}")
        return descriptor, before
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise BaselineValidationError(f"baseline file is missing: {relative}") from exc
    except OSError as exc:
        raise BaselineValidationError(f"cannot safely read baseline file {relative}: {exc}") from exc
    finally:
        for directory in reversed(descriptors):
            os.close(directory)


def _read_regular_file(root: Path, relative: str, maximum: int) -> tuple[bytes, os.stat_result]:
    descriptor, before = _open_baseline_file(root, relative)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, COPY_BUFFER_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise BaselineValidationError(
                    f"baseline file exceeds the supported size limit: {relative}"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _entry_signature(before) != _entry_signature(after) or total != before.st_size:
        raise BaselineValidationError(f"baseline file changed while reading: {relative}")
    return b"".join(chunks), after


def _digest_regular_file(root: Path, relative: str) -> tuple[str, int, os.stat_result]:
    descriptor, before = _open_baseline_file(root, relative)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, COPY_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _entry_signature(before) != _entry_signature(after) or total != before.st_size:
        raise BaselineValidationError(f"baseline file changed while hashing: {relative}")
    return digest.hexdigest(), total, after


def _load_index_manifest(root: Path, family: str) -> dict[str, object]:
    relative = f"assets/{family}/manifest.json"
    raw, _ = _read_regular_file(root, relative, MAX_INDEX_MANIFEST_BYTES)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BaselineValidationError(f"{family} manifest is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BaselineValidationError(f"{family} manifest is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BaselineValidationError(f"{family} manifest must be a JSON object")
    return value


def _manifest_shard_paths(family: str, manifest: Mapping[str, object]) -> set[str]:
    shards = manifest.get("shards")
    if not isinstance(shards, dict) or not shards:
        raise BaselineValidationError(
            f"{family} manifest shards must be a nonempty JSON object"
        )
    paths: set[str] = set()
    for key, count in shards.items():
        if not isinstance(key, str) or INDEX_SHARD_KEY_RE.fullmatch(key) is None:
            raise BaselineValidationError(
                f"{family} manifest contains an invalid shard key: {key!r}"
            )
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or count > MAX_SAFE_INTEGER
        ):
            raise BaselineValidationError(
                f"{family} manifest shard {key!r} must have a positive integer count"
            )
        paths.add(f"assets/{family}/{key}.json")
    return paths


def _validate_index_closure(root: Path, files: Sequence[PublicAssetFile]) -> None:
    inventory = {item.path for item in files}
    for family in INDEX_FAMILY_NAMES:
        prefix = f"assets/{family}/"
        manifest_path = prefix + "manifest.json"
        manifest = _load_index_manifest(root, family)
        expected = {manifest_path, *_manifest_shard_paths(family, manifest)}
        if family == "premise-index":
            chunks = manifest.get("chunks")
            if (
                isinstance(chunks, bool)
                or not isinstance(chunks, int)
                or chunks < 0
                or chunks > MAX_SAFE_INTEGER
            ):
                raise BaselineValidationError(
                    "premise-index manifest chunks must be a non-negative integer"
                )
            # Bound iteration before materializing the declared range.  Every
            # chunk must correspond to one inventory entry, so a larger value
            # is necessarily invalid.
            family_file_count = sum(path.startswith(prefix) for path in inventory)
            if chunks > family_file_count:
                raise BaselineValidationError(
                    "premise-index manifest declares more name chunks than baseline files"
                )
            expected.update(f"{prefix}names/{index}.json" for index in range(chunks))

        actual = {path for path in inventory if path.startswith(prefix)}
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise BaselineValidationError(
                f"{family} payload does not close over its manifest ({'; '.join(details)})"
            )


def _scan_frozen_tree(root: Path) -> tuple[dict[str, os.stat_result], set[str]]:
    result: dict[str, os.stat_result] = {}
    directories: set[str] = set()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags)

    def walk(directory_fd: int, prefix: str, directory_info: os.stat_result) -> None:
        if stat.S_IMODE(directory_info.st_mode) != 0o555:
            raise BaselineValidationError(
                f"baseline directory permissions must be 0555: {prefix or '.'}"
            )
        before = _list_directory(directory_fd, prefix or ".")
        for name, discovered in before:
            relative = f"{prefix}/{name}" if prefix else name
            mode = discovered.st_mode
            if stat.S_ISLNK(mode):
                raise BaselineValidationError(f"baseline contains a symlink: {relative}")
            if stat.S_ISDIR(mode):
                if len(relative.encode("utf-8")) > MAX_PATH_BYTES or len(relative.split("/")) > MAX_PATH_COMPONENTS:
                    raise BaselineValidationError(
                        f"baseline directory path exceeds supported limits: {relative!r}"
                    )
                directories.add(relative)
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if opened.st_dev != discovered.st_dev or opened.st_ino != discovered.st_ino:
                        raise BaselineValidationError(
                            f"baseline directory changed while opening: {relative}"
                        )
                    walk(child_fd, relative, opened)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(mode):
                raise BaselineValidationError(f"baseline contains a non-regular file: {relative}")
            if relative != MANIFEST_NAME:
                _validate_relative_path(relative, "baseline file path")
            if stat.S_IMODE(mode) != 0o444:
                raise BaselineValidationError(
                    f"baseline file permissions must be 0444: {relative}"
                )
            if discovered.st_nlink != 1:
                raise BaselineValidationError(
                    f"baseline files must not have external hard links: {relative}"
                )
            result[relative] = discovered
        after = _list_directory(directory_fd, prefix or ".")
        if not _same_listing(before, after):
            raise BaselineValidationError(
                f"baseline directory changed while verifying: {prefix or '.'}"
            )

    try:
        walk(root_fd, "", os.fstat(root_fd))
    finally:
        os.close(root_fd)
    return result, directories


def verify_public_baseline(
    baseline_root: os.PathLike[str] | str,
    repo_root: os.PathLike[str] | str,
    *,
    expected_baseline_id: str | None = None,
    expected_authority_git_commit: str | None = None,
) -> PublicAssetBaseline:
    """Verify a sealed baseline, its identity, complete inventory, and modes."""

    repository = _resolve_repo(repo_root)
    root = _resolve_baseline_root(baseline_root, repository)
    actual, actual_directories = _scan_frozen_tree(root)
    if MANIFEST_NAME not in actual:
        raise BaselineValidationError(f"baseline is missing {MANIFEST_NAME}")
    manifest_raw, _ = _read_regular_file(root, MANIFEST_NAME, MAX_MANIFEST_BYTES)
    document = _load_manifest_bytes(manifest_raw)
    baseline_id, authority, files = _validate_manifest(document)
    attested_files = _load_source_attestation(repository, authority)
    _require_attested_source(files, attested_files, authority)
    baseline_hex = BASELINE_ID_RE.fullmatch(baseline_id).group(1)  # type: ignore[union-attr]
    if root.name != baseline_hex:
        raise BaselineValidationError(
            f"baseline root basename {root.name} does not match baseline_id {baseline_hex}"
        )
    if expected_baseline_id is not None and baseline_id != expected_baseline_id:
        raise BaselineValidationError(
            f"baseline identity mismatch: expected {expected_baseline_id}, got {baseline_id}"
        )
    if expected_authority_git_commit is not None:
        expected_commit = _validate_commit(
            expected_authority_git_commit,
            "expected authority Git commit",
        )
        if authority != expected_commit:
            raise BaselineValidationError(
                f"baseline authority mismatch: expected {expected_commit}, got {authority}"
            )

    expected_paths = {MANIFEST_NAME, *(item.path for item in files)}
    actual_paths = set(actual)
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unlisted " + ", ".join(unexpected))
        raise BaselineValidationError(
            "baseline tree does not match its complete inventory (" + "; ".join(details) + ")"
        )
    expected_directories: set[str] = set()
    for path in expected_paths:
        parts = PurePosixPath(path).parts
        for length in range(1, len(parts)):
            expected_directories.add("/".join(parts[:length]))
    if actual_directories != expected_directories:
        missing_directories = sorted(expected_directories - actual_directories)
        unexpected_directories = sorted(actual_directories - expected_directories)
        details = []
        if missing_directories:
            details.append("missing directories " + ", ".join(missing_directories))
        if unexpected_directories:
            details.append("unlisted directories " + ", ".join(unexpected_directories))
        raise BaselineValidationError(
            "baseline directory tree does not match its inventory (" + "; ".join(details) + ")"
        )

    _validate_index_closure(root, files)

    for item in files:
        digest, size, info = _digest_regular_file(root, item.path)
        if size != item.bytes or info.st_size != item.bytes:
            raise BaselineValidationError(
                f"baseline byte count mismatch for {item.path}: expected {item.bytes}, got {size}"
            )
        if digest != item.sha256:
            raise BaselineValidationError(
                f"baseline digest mismatch for {item.path}: expected {item.sha256}, got {digest}"
            )

    final_actual, final_directories = _scan_frozen_tree(root)
    if final_directories != actual_directories or {
        path: _entry_signature(info) for path, info in final_actual.items()
    } != {path: _entry_signature(info) for path, info in actual.items()}:
        raise BaselineValidationError("baseline tree changed while verifying")

    return PublicAssetBaseline(
        root=root,
        manifest_path=root / MANIFEST_NAME,
        baseline_id=baseline_id,
        baseline_hex=baseline_hex,
        authority_git_commit=authority,
        files=files,
        total_bytes=sum(item.bytes for item in files),
    )


def render_source_attestation(
    source_public_root: os.PathLike[str] | str,
) -> bytes:
    """Render the canonical inventory that must be reviewed and committed.

    Rendering does not make the source trustworthy by itself.  The resulting
    bytes become authoritative only after they are reviewed and committed at
    :data:`SOURCE_ATTESTATION_PATH`; ``freeze`` always reloads them from the
    named Git commit.
    """

    source = _resolve_source_root(source_public_root)
    files = _inventory_source_tree(source)
    _validate_required_payload(files)
    _validate_index_closure(source, files)
    document = {
        "schema": SOURCE_ATTESTATION_SCHEMA,
        "files": [
            {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
            for item in files
        ],
    }
    # Keep the producer and consumer on exactly the same strict contract.
    loaded = _load_manifest_bytes(_canonical_json_bytes(document))
    _exact_keys(loaded, _SOURCE_ATTESTATION_KEYS, "source attestation")
    _validate_file_inventory(loaded["files"], label="source attestation")
    return _canonical_json_bytes(document)


def freeze_public_baseline(
    source_public_root: os.PathLike[str] | str,
    baseline_store: os.PathLike[str] | str,
    authority_git_commit: str,
    repo_root: os.PathLike[str] | str,
) -> PublicAssetBaseline:
    """Freeze a complete non-Brain public tree into an immutable external store."""

    repository = _resolve_repo(repo_root)
    source = _resolve_source_root(source_public_root)
    authority = _validate_commit(authority_git_commit)
    attested_files = _load_source_attestation(repository, authority)
    prospective_store = _prospective_store(baseline_store, repository).resolve(strict=False)
    if (
        source == prospective_store
        or _is_relative_to(source, prospective_store)
        or _is_relative_to(prospective_store, source)
    ):
        raise BaselineValidationError("source public root and baseline store must not overlap")
    store = _prepare_store(baseline_store, repository)
    if source == store or _is_relative_to(source, store) or _is_relative_to(store, source):
        raise BaselineValidationError("source public root and baseline store must not overlap")

    pending = store / f".pending-{os.getpid()}-{uuid.uuid4().hex}"
    with _store_lock(store):
        try:
            pending.mkdir(mode=0o700)
            files = _copy_source_tree(source, pending)
            _validate_required_payload(files)
            _validate_index_closure(pending, files)
            _require_attested_source(files, attested_files, authority)
            identity = _identity_document(authority, files)
            baseline_id = _baseline_id(identity)
            baseline_hex = BASELINE_ID_RE.fullmatch(baseline_id).group(1)  # type: ignore[union-attr]
            manifest = {**identity, "baseline_id": baseline_id}
            # Exercise the same strict validator before publication.
            _validate_manifest(manifest)
            _write_manifest(pending / MANIFEST_NAME, manifest)
            _seal_pending_tree(pending)

            final = store / baseline_hex
            if final.exists() or final.is_symlink():
                existing = verify_public_baseline(
                    final,
                    repository,
                    expected_baseline_id=baseline_id,
                    expected_authority_git_commit=authority,
                )
                _remove_pending(pending)
                return existing
            try:
                os.rename(pending, final)
                _fsync_directory(store)
            except OSError as exc:
                # A cooperating concurrent freezer may have won between the
                # existence check and rename.  Accept only the exact artifact.
                if final.exists() and not final.is_symlink():
                    existing = verify_public_baseline(
                        final,
                        repository,
                        expected_baseline_id=baseline_id,
                        expected_authority_git_commit=authority,
                    )
                    _remove_pending(pending)
                    return existing
                raise BaselineFreezeError(
                    f"cannot atomically publish baseline {baseline_id}: {exc}"
                ) from exc
            return verify_public_baseline(
                final,
                repository,
                expected_baseline_id=baseline_id,
                expected_authority_git_commit=authority,
            )
        except Exception:
            _remove_pending(pending)
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze_parser = subparsers.add_parser("freeze", help="freeze a non-Brain public tree")
    freeze_parser.add_argument("--source-public", type=Path, required=True)
    freeze_parser.add_argument("--store", type=Path, required=True)
    freeze_parser.add_argument("--repo-root", type=Path, required=True)
    freeze_parser.add_argument("--authority-git-commit", required=True)

    attest_parser = subparsers.add_parser(
        "attest",
        help="render the canonical source inventory for review and commit",
    )
    attest_parser.add_argument("--source-public", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify", help="verify one frozen baseline")
    verify_parser.add_argument("--baseline", type=Path, required=True)
    verify_parser.add_argument("--repo-root", type=Path, required=True)
    verify_parser.add_argument("--expected-baseline-id")
    verify_parser.add_argument("--expected-authority-git-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "attest":
            sys.stdout.buffer.write(render_source_attestation(args.source_public))
            return 0
        if args.command == "freeze":
            result = freeze_public_baseline(
                args.source_public,
                args.store,
                args.authority_git_commit,
                args.repo_root,
            )
        else:
            result = verify_public_baseline(
                args.baseline,
                args.repo_root,
                expected_baseline_id=args.expected_baseline_id,
                expected_authority_git_commit=args.expected_authority_git_commit,
            )
    except (OSError, PublicBaselineError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result.summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
