#!/usr/bin/env python3
"""Fail-closed, revision-pinned Hugging Face dataset acquisition.

Sidecars prove that local bytes are internally consistent; they do not prove
that bytes belonged to a Hugging Face commit. That authority comes from the
reviewed, checked-in huggingface_pins.json revision/size/SHA-256 registry.

Writers serialize acquisition without blocking readers from the prior
generation, then take the dataset publication lock only for recovery and the
short commit. Publication is protected by a durable rollback journal and
hard-link backups. Readers hold a shared publication lock while parsing
verified files, so they never observe a mixed multi-file generation.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

SIDECAR_SCHEMA = "wikilean.huggingface-file/v1"
PIN_REGISTRY_SCHEMA = "wikilean.huggingface-pins/v1"
JOURNAL_SCHEMA = "wikilean.huggingface-publication-journal/v1"
REVISION_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
DATASET_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SIDECAR_SUFFIX = ".hf-source.json"
SIDECAR_KEYS = frozenset(
    {"schema", "dataset", "revision", "file_url", "sha256", "size"}
)
DEFAULT_PIN_REGISTRY = Path(__file__).with_name("huggingface_pins.json")


class HuggingFaceArtifactError(RuntimeError):
    """The requested artifact could not be acquired or verified safely."""


@dataclass(frozen=True, slots=True)
class ArtifactRequest:
    remote_path: str
    destination: Path
    expected_sha256: str
    expected_size: int


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    destination: Path
    metadata: dict[str, object]
    downloaded: bool


@dataclass(frozen=True, slots=True)
class ReviewedDatasetPin:
    dataset: str
    revision: str
    verified_at: str
    verification: str
    files: dict[str, tuple[str, int]]

    def request(self, remote_path: str, destination: Path) -> ArtifactRequest:
        try:
            digest, size = self.files[remote_path]
        except KeyError as exc:
            raise HuggingFaceArtifactError(
                f"{self.dataset} file is not in the reviewed pin registry: "
                f"{remote_path}"
            ) from exc
        return ArtifactRequest(remote_path, destination, digest, size)


@dataclass(slots=True)
class _StagedArtifact:
    request: ArtifactRequest
    data_path: Path
    sidecar_path: Path
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Replacement:
    target: Path
    staged: Path
    new_sha256: str
    new_size: int


def validate_revision(value: str | None) -> str:
    """Return a normalized immutable Hugging Face git commit id."""
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        raise HuggingFaceArtifactError(
            "Hugging Face revision must be an explicit full 40-hex commit id; "
            "branches and tags such as 'main' are not accepted"
        )
    return value.lower()


def resolve_revision(value: str | None, *, environment_variable: str) -> str:
    """Validate a CLI/env revision value and include the source in errors."""
    if value is None or not value.strip():
        raise HuggingFaceArtifactError(
            f"an immutable dataset revision is required: pass --revision or set "
            f"{environment_variable} to a full 40-hex Hugging Face commit id"
        )
    try:
        return validate_revision(value.strip())
    except HuggingFaceArtifactError as exc:
        raise HuggingFaceArtifactError(
            f"invalid --revision/{environment_variable}: {exc}"
        ) from exc


def load_reviewed_pin(
    dataset: str,
    *,
    registry_path: Path = DEFAULT_PIN_REGISTRY,
) -> ReviewedDatasetPin:
    """Load one exact dataset pin from the reviewed repository registry."""
    _validate_dataset(dataset)
    registry = _load_json_object(
        registry_path, label="Hugging Face pin registry"
    )
    if set(registry) != {"schema", "datasets"}:
        raise HuggingFaceArtifactError(
            f"pin registry has unexpected keys: {registry_path}"
        )
    if registry.get("schema") != PIN_REGISTRY_SCHEMA:
        raise HuggingFaceArtifactError(
            f"unsupported Hugging Face pin registry schema: {registry_path}"
        )
    datasets = registry.get("datasets")
    if not isinstance(datasets, dict):
        raise HuggingFaceArtifactError(
            f"pin registry datasets must be an object: {registry_path}"
        )
    try:
        raw = datasets[dataset]
    except KeyError as exc:
        raise HuggingFaceArtifactError(
            f"dataset has no reviewed immutable pin: {dataset}"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "revision",
        "verified_at",
        "verification",
        "files",
    }:
        raise HuggingFaceArtifactError(
            f"reviewed pin has an invalid shape for dataset {dataset}"
        )
    revision = validate_revision(raw.get("revision"))
    verified_at = raw.get("verified_at")
    verification = raw.get("verification")
    if not isinstance(verified_at, str) or not verified_at:
        raise HuggingFaceArtifactError(
            f"reviewed pin verified_at is invalid for dataset {dataset}"
        )
    if not isinstance(verification, str) or not verification:
        raise HuggingFaceArtifactError(
            f"reviewed pin verification is invalid for dataset {dataset}"
        )
    raw_files = raw.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise HuggingFaceArtifactError(
            f"reviewed pin files are missing for dataset {dataset}"
        )
    files: dict[str, tuple[str, int]] = {}
    for remote_path, entry in raw_files.items():
        build_file_url(dataset, revision, remote_path)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size"}
            or not isinstance(entry.get("sha256"), str)
            or not SHA256_RE.fullmatch(entry["sha256"])
            or type(entry.get("size")) is not int
            or entry["size"] < 0
        ):
            raise HuggingFaceArtifactError(
                f"reviewed file pin is invalid for {dataset}/{remote_path}"
            )
        files[remote_path] = (entry["sha256"], entry["size"])
    return ReviewedDatasetPin(
        dataset, revision, verified_at, verification, files
    )


def require_reviewed_revision(
    supplied_revision: str,
    pin: ReviewedDatasetPin,
) -> str:
    revision = validate_revision(supplied_revision)
    if revision != pin.revision:
        raise HuggingFaceArtifactError(
            f"revision {revision} is not the reviewed pin for {pin.dataset}; "
            f"update and review {DEFAULT_PIN_REGISTRY.name} first"
        )
    return revision


def build_file_url(dataset: str, revision: str, remote_path: str) -> str:
    """Construct an exact-revision Hugging Face dataset file URL."""
    _validate_dataset(dataset)
    revision = validate_revision(revision)
    if not isinstance(remote_path, str) or not remote_path:
        raise HuggingFaceArtifactError(
            "remote path must be a non-empty relative path"
        )
    parts = remote_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HuggingFaceArtifactError(
            "remote path must not be absolute or contain dot segments"
        )
    encoded_dataset = "/".join(
        quote(part, safe="-._~") for part in dataset.split("/")
    )
    encoded_path = "/".join(quote(part, safe="-._~") for part in parts)
    return (
        f"https://huggingface.co/datasets/{encoded_dataset}/resolve/"
        f"{revision}/{encoded_path}"
    )


def sidecar_path(destination: Path) -> Path:
    return destination.with_name(destination.name + SIDECAR_SUFFIX)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_cached_artifact(
    request: ArtifactRequest,
    *,
    dataset: str,
    revision: str,
) -> dict[str, object]:
    """Verify cached bytes, reviewed expectations, and the local sidecar."""
    _validate_request(request)
    revision = validate_revision(revision)
    destination = Path(request.destination)
    metadata_file = sidecar_path(destination)
    _require_regular_file(destination, label="cached artifact")
    metadata = _load_metadata(metadata_file)
    expected_url = build_file_url(dataset, revision, request.remote_path)
    expected_fields: dict[str, object] = {
        "dataset": dataset,
        "revision": revision,
        "file_url": expected_url,
        "sha256": request.expected_sha256,
        "size": request.expected_size,
    }
    for key, expected in expected_fields.items():
        if metadata.get(key) != expected:
            raise HuggingFaceArtifactError(
                f"cached artifact metadata {key} mismatch for {destination}: "
                f"expected {expected!r}, found {metadata.get(key)!r}"
            )
    digest, size = sha256_file(destination)
    if digest != request.expected_sha256 or size != request.expected_size:
        raise HuggingFaceArtifactError(
            f"cached artifact bytes do not match the reviewed pin: {destination}"
        )
    return metadata


@contextlib.contextmanager
def verified_artifact_set(
    *,
    dataset: str,
    revision: str,
    requests: Sequence[ArtifactRequest],
) -> Iterator[list[dict[str, object]]]:
    """Hold a shared dataset lock while callers consume verified files."""
    normalized, parent = _normalize_requests(dataset, revision, requests)
    with _reader_lock(parent, dataset):
        metadata = [
            verify_cached_artifact(
                request, dataset=dataset, revision=revision
            )
            for request in normalized
        ]
        yield metadata


@contextlib.contextmanager
def verified_reviewed_dataset(
    dataset: str,
    files: dict[str, Path],
    *,
    optional_files: dict[str, Path] | None = None,
) -> Iterator[tuple[ReviewedDatasetPin, dict[str, dict[str, object]]]]:
    """Open a reviewed local dataset generation under one shared lock."""
    pin = load_reviewed_pin(dataset)
    required_requests = [
        pin.request(remote_path, destination)
        for remote_path, destination in sorted(files.items())
    ]
    optional_requests = [
        pin.request(remote_path, destination)
        for remote_path, destination in sorted((optional_files or {}).items())
    ]
    all_requests = required_requests + optional_requests
    normalized, parent = _normalize_requests(
        dataset, pin.revision, all_requests
    )
    with _reader_lock(parent, dataset):
        metadata: list[dict[str, object]] = []
        present_requests: list[ArtifactRequest] = []
        for request in normalized:
            is_optional = request in optional_requests
            artifact_exists = _path_exists(request.destination)
            metadata_exists = _path_exists(
                sidecar_path(request.destination)
            )
            if is_optional and not artifact_exists and not metadata_exists:
                continue
            if is_optional and artifact_exists != metadata_exists:
                raise HuggingFaceArtifactError(
                    f"incomplete optional Hugging Face artifact: "
                    f"{request.destination}"
                )
            metadata.append(
                verify_cached_artifact(
                    request, dataset=dataset, revision=pin.revision
                )
            )
            present_requests.append(request)
        yield pin, {
            request.remote_path: item
            for request, item in zip(
                present_requests, metadata, strict=True
            )
        }


@contextlib.contextmanager
def optional_verified_reviewed_dataset(
    dataset: str,
    files: dict[str, Path],
) -> Iterator[
    tuple[ReviewedDatasetPin, dict[str, dict[str, object]]] | None
]:
    """Yield None only when every artifact and sidecar is cleanly absent."""
    pin = load_reviewed_pin(dataset)
    requests = [
        pin.request(remote_path, destination)
        for remote_path, destination in sorted(files.items())
    ]
    normalized, parent = _normalize_requests(
        dataset, pin.revision, requests
    )
    with _reader_lock(parent, dataset):
        paths = [
            path
            for request in normalized
            for path in (
                request.destination,
                sidecar_path(request.destination),
            )
        ]
        present = [_path_exists(path) for path in paths]
        if not any(present):
            yield None
            return
        if not all(present):
            raise HuggingFaceArtifactError(
                f"incomplete optional Hugging Face cache for {dataset}"
            )
        metadata = [
            verify_cached_artifact(
                request, dataset=dataset, revision=pin.revision
            )
            for request in normalized
        ]
        yield pin, {
            request.remote_path: item
            for request, item in zip(requests, metadata, strict=True)
        }


def adopt_existing_artifacts(
    *,
    dataset: str,
    revision: str,
    requests: Sequence[ArtifactRequest],
) -> list[ArtifactResult]:
    """Add sidecars to legacy caches only after reviewed digest verification."""
    revision = validate_revision(revision)
    normalized, parent = _normalize_requests(dataset, revision, requests)
    staged: list[_StagedArtifact] = []
    with _acquisition_lock(parent, dataset):
        with _exclusive_publication_lock(parent, dataset):
            _recover_publication(parent, dataset)
            _cleanup_orphan_work(parent, dataset)
        try:
            for request in normalized:
                _require_regular_file(
                    request.destination, label="legacy cached artifact"
                )
                digest, size = sha256_file(request.destination)
                if (
                    digest != request.expected_sha256
                    or size != request.expected_size
                ):
                    raise HuggingFaceArtifactError(
                        f"legacy cache does not match reviewed pin: "
                        f"{request.destination}"
                    )
                metadata = _metadata_for(dataset, revision, request)
                staged.append(
                    _stage_sidecar(
                        dataset=dataset,
                        request=request,
                        metadata=metadata,
                    )
                )
            with _exclusive_publication_lock(parent, dataset):
                _recover_publication(parent, dataset)
                _publish_staged(
                    staged,
                    parent=parent,
                    dataset=dataset,
                    sidecars_only=True,
                )
            return [
                ArtifactResult(
                    request.destination,
                    verify_cached_artifact(
                        request, dataset=dataset, revision=revision
                    ),
                    False,
                )
                for request in normalized
            ]
        finally:
            _cleanup_staged(staged)


def fetch_huggingface_artifacts(
    *,
    dataset: str,
    revision: str,
    requests: Sequence[ArtifactRequest],
    user_agent: str,
    force: bool = False,
    timeout_seconds: int = 3600,
    retries: int = 3,
    runner: Callable[..., object] = subprocess.run,
) -> list[ArtifactResult]:
    """Fetch and crash-safely publish one exact reviewed dataset generation."""
    revision = validate_revision(revision)
    normalized, parent = _normalize_requests(dataset, revision, requests)
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise HuggingFaceArtifactError(
            "timeout_seconds must be a positive integer"
        )
    if type(retries) is not int or retries < 0:
        raise HuggingFaceArtifactError(
            "retries must be a non-negative integer"
        )
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise HuggingFaceArtifactError("user_agent must be non-empty")

    results: dict[Path, ArtifactResult] = {}
    staged: list[_StagedArtifact] = []
    with _acquisition_lock(parent, dataset):
        with _exclusive_publication_lock(parent, dataset):
            _recover_publication(parent, dataset)
            _cleanup_orphan_work(parent, dataset)
        try:
            for request in normalized:
                if _path_exists(request.destination) and not force:
                    metadata = verify_cached_artifact(
                        request, dataset=dataset, revision=revision
                    )
                    results[request.destination] = ArtifactResult(
                        request.destination, metadata, False
                    )
                else:
                    staged.append(
                        _stage_artifact(
                            dataset=dataset,
                            revision=revision,
                            request=request,
                            user_agent=user_agent,
                            timeout_seconds=timeout_seconds,
                            retries=retries,
                            runner=runner,
                        )
                    )
            with _exclusive_publication_lock(parent, dataset):
                _recover_publication(parent, dataset)
                _publish_staged(
                    staged,
                    parent=parent,
                    dataset=dataset,
                    sidecars_only=False,
                )
            for item in staged:
                metadata = verify_cached_artifact(
                    item.request, dataset=dataset, revision=revision
                )
                results[item.request.destination] = ArtifactResult(
                    item.request.destination, metadata, True
                )
        finally:
            _cleanup_staged(staged)
    return [results[request.destination] for request in normalized]


def _normalize_requests(
    dataset: str,
    revision: str,
    requests: Sequence[ArtifactRequest],
) -> tuple[list[ArtifactRequest], Path]:
    _validate_dataset(dataset)
    validate_revision(revision)
    if not requests:
        raise HuggingFaceArtifactError(
            "at least one artifact request is required"
        )
    normalized: list[ArtifactRequest] = []
    parents: set[Path] = set()
    claimed_paths: set[Path] = set()
    for request in requests:
        destination = Path(request.destination)
        normalized_request = ArtifactRequest(
            request.remote_path,
            destination,
            request.expected_sha256,
            request.expected_size,
        )
        _validate_request(normalized_request)
        build_file_url(dataset, revision, normalized_request.remote_path)
        parent = destination.parent.absolute()
        parents.add(parent)
        for claimed in (
            destination.absolute(),
            sidecar_path(destination).absolute(),
        ):
            if claimed in claimed_paths:
                raise HuggingFaceArtifactError(
                    f"artifact/sidecar target collision: {claimed}"
                )
            claimed_paths.add(claimed)
        normalized.append(normalized_request)
    if len(parents) != 1:
        raise HuggingFaceArtifactError(
            "one atomic dataset publication cannot span cache directories"
        )
    parent = next(iter(parents))
    lock, journal = _state_paths(parent, dataset)
    acquisition_lock = _acquisition_lock_path(parent, dataset)
    if any(
        path.absolute() in claimed_paths
        for path in (lock, acquisition_lock, journal)
    ):
        raise HuggingFaceArtifactError(
            "artifact target collides with dataset lock or journal"
        )
    temporary_prefix = f".hf-{_dataset_token(dataset)}."
    if any(
        path.name.startswith(temporary_prefix)
        and path.name.endswith(".tmp")
        for path in claimed_paths
    ):
        raise HuggingFaceArtifactError(
            "artifact target collides with dataset transaction namespace"
        )
    return normalized, parent


def _stage_artifact(
    *,
    dataset: str,
    revision: str,
    request: ArtifactRequest,
    user_agent: str,
    timeout_seconds: int,
    retries: int,
    runner: Callable[..., object],
) -> _StagedArtifact:
    destination = request.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    data_path = _temporary_path(destination, "download", dataset)
    sidecar_temp: Path | None = None
    try:
        url = build_file_url(dataset, revision, request.remote_path)
        command = [
            "curl",
            "--disable",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(timeout_seconds),
            "--retry",
            str(retries),
            "--retry-all-errors",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--header",
            f"User-Agent: {user_agent}",
            "--output",
            str(data_path),
            url,
        ]
        try:
            completed = runner(command, check=False)
        except OSError as exc:
            raise HuggingFaceArtifactError(
                f"could not launch curl for {request.remote_path}: {exc}"
            ) from exc
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise HuggingFaceArtifactError(
                f"download failed for {request.remote_path} "
                f"(curl rc={returncode!r})"
            )
        _fsync_file(data_path)
        digest, size = sha256_file(data_path)
        if digest != request.expected_sha256 or size != request.expected_size:
            raise HuggingFaceArtifactError(
                f"download does not match reviewed pin for "
                f"{request.remote_path}: got {size} bytes/{digest}"
            )
        metadata = _metadata_for(dataset, revision, request)
        staged_sidecar = _stage_sidecar(
            dataset=dataset,
            request=request,
            metadata=metadata,
        )
        sidecar_temp = staged_sidecar.sidecar_path
        return _StagedArtifact(
            request, data_path, sidecar_temp, metadata
        )
    except BaseException:
        _unlink_if_exists(data_path)
        if sidecar_temp is not None:
            _unlink_if_exists(sidecar_temp)
        raise


def _stage_sidecar(
    *,
    dataset: str,
    request: ArtifactRequest,
    metadata: dict[str, object],
) -> _StagedArtifact:
    destination = request.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    sidecar_temp = _temporary_path(destination, "metadata", dataset)
    try:
        with sidecar_temp.open("w", encoding="utf-8") as handle:
            json.dump(
                metadata, handle, ensure_ascii=False, indent=2, sort_keys=True
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return _StagedArtifact(
            request, destination, sidecar_temp, metadata
        )
    except BaseException:
        _unlink_if_exists(sidecar_temp)
        raise


def _publish_staged(
    staged: Sequence[_StagedArtifact],
    *,
    parent: Path,
    dataset: str,
    sidecars_only: bool,
) -> None:
    if not staged:
        return
    replacements: list[_Replacement] = []
    for item in staged:
        if not sidecars_only:
            replacements.append(
                _Replacement(
                    item.request.destination,
                    item.data_path,
                    item.request.expected_sha256,
                    item.request.expected_size,
                )
            )
        sidecar_digest, sidecar_size = sha256_file(item.sidecar_path)
        replacements.append(
            _Replacement(
                sidecar_path(item.request.destination),
                item.sidecar_path,
                sidecar_digest,
                sidecar_size,
            )
        )
    _publish_replacements(
        replacements, parent=parent, dataset=dataset
    )


def _publish_replacements(
    replacements: Sequence[_Replacement],
    *,
    parent: Path,
    dataset: str,
) -> None:
    journal_path = _state_paths(parent, dataset)[1]
    entries: list[dict[str, object]] = []
    try:
        for replacement in replacements:
            target = replacement.target
            staged = replacement.staged
            if target.parent.absolute() != parent:
                raise HuggingFaceArtifactError(
                    f"publication target escaped cache directory: {target}"
                )
            _require_regular_or_absent(target, label="publication target")
            _require_regular_file(staged, label="staged publication file")
            backup = _hardlink_backup(target, dataset)
            old_sha256: str | None = None
            old_size: int | None = None
            if backup is not None:
                old_sha256, old_size = sha256_file(backup)
            entries.append(
                {
                    "target": target.name,
                    "staged": staged.name,
                    "backup": backup.name if backup is not None else None,
                    "old_sha256": old_sha256,
                    "old_size": old_size,
                    "new_sha256": replacement.new_sha256,
                    "new_size": replacement.new_size,
                }
            )
        journal = {
            "schema": JOURNAL_SCHEMA,
            "dataset": dataset,
            "phase": "prepared",
            "entries": entries,
        }
        _write_journal(journal_path, journal, dataset)
        for replacement in replacements:
            os.replace(replacement.staged, replacement.target)
        _fsync_directory(parent)
        for replacement in replacements:
            _verify_digest(
                replacement.target,
                replacement.new_sha256,
                replacement.new_size,
                label="published artifact",
            )
        journal["phase"] = "committed"
        _write_journal(journal_path, journal, dataset)
        _finish_committed_journal(parent, journal_path, journal)
    except BaseException as exc:
        if _path_exists(journal_path):
            try:
                _recover_publication(parent, dataset)
            except BaseException as recovery_exc:
                raise HuggingFaceArtifactError(
                    f"artifact publication failed ({exc}); durable recovery "
                    f"also failed: {recovery_exc}"
                ) from exc
        else:
            for entry in entries:
                backup_name = entry.get("backup")
                if isinstance(backup_name, str):
                    _unlink_if_exists(parent / backup_name)
            _cleanup_orphan_work(parent, dataset)
        if isinstance(exc, HuggingFaceArtifactError):
            raise
        raise HuggingFaceArtifactError(
            f"artifact publication failed: {exc}"
        ) from exc


def _recover_publication(parent: Path, dataset: str) -> None:
    journal_path = _state_paths(parent, dataset)[1]
    if not _path_exists(journal_path):
        return
    journal = _load_json_object(
        journal_path, label="Hugging Face publication journal"
    )
    if (
        set(journal) != {"schema", "dataset", "phase", "entries"}
        or journal.get("schema") != JOURNAL_SCHEMA
        or journal.get("dataset") != dataset
        or journal.get("phase") not in {"prepared", "committed"}
        or not isinstance(journal.get("entries"), list)
        or not journal["entries"]
    ):
        raise HuggingFaceArtifactError(
            f"invalid publication journal: {journal_path}"
        )
    entries = [
        _validate_journal_entry(parent, entry)
        for entry in journal["entries"]
    ]
    if journal["phase"] == "committed":
        for entry in entries:
            _verify_digest(
                entry["target"],
                entry["new_sha256"],
                entry["new_size"],
                label="committed publication target",
            )
        _finish_committed_journal(parent, journal_path, journal)
        return

    for entry in reversed(entries):
        target: Path = entry["target"]
        staged: Path = entry["staged"]
        backup: Path | None = entry["backup"]
        if backup is not None and _path_exists(backup):
            _verify_digest(
                backup,
                entry["old_sha256"],
                entry["old_size"],
                label="publication rollback backup",
            )
            os.replace(backup, target)
            _unlink_if_exists(backup)
        elif backup is not None:
            _verify_digest(
                target,
                entry["old_sha256"],
                entry["old_size"],
                label="already-restored publication target",
            )
        elif _path_exists(staged):
            if _path_exists(target):
                raise HuggingFaceArtifactError(
                    f"unexpected target appeared during recovery: {target}"
                )
        elif _path_exists(target):
            _verify_digest(
                target,
                entry["new_sha256"],
                entry["new_size"],
                label="partially published target",
            )
            target.unlink()
        _unlink_if_exists(staged)
    _fsync_directory(parent)
    journal_path.unlink()
    _fsync_directory(parent)


def _finish_committed_journal(
    parent: Path,
    journal_path: Path,
    journal: dict[str, object],
) -> None:
    for raw_entry in journal["entries"]:
        entry = _validate_journal_entry(parent, raw_entry)
        backup = entry["backup"]
        if backup is not None:
            _unlink_if_exists(backup)
        _unlink_if_exists(entry["staged"])
    _fsync_directory(parent)
    journal_path.unlink()
    _fsync_directory(parent)


def _validate_journal_entry(
    parent: Path,
    value: object,
) -> dict[str, object]:
    keys = {
        "target",
        "staged",
        "backup",
        "old_sha256",
        "old_size",
        "new_sha256",
        "new_size",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise HuggingFaceArtifactError(
            "publication journal entry is invalid"
        )
    target = _journal_member(parent, value["target"], "target")
    staged = _journal_member(parent, value["staged"], "staged")
    backup_value = value["backup"]
    backup = (
        None
        if backup_value is None
        else _journal_member(parent, backup_value, "backup")
    )
    digest = value["new_sha256"]
    size = value["new_size"]
    if (
        not isinstance(digest, str)
        or not SHA256_RE.fullmatch(digest)
        or type(size) is not int
        or size < 0
    ):
        raise HuggingFaceArtifactError(
            "publication journal new digest is invalid"
        )
    old_digest = value["old_sha256"]
    old_size = value["old_size"]
    if backup is None:
        if old_digest is not None or old_size is not None:
            raise HuggingFaceArtifactError(
                "publication journal has old metadata without a backup"
            )
    elif (
        not isinstance(old_digest, str)
        or not SHA256_RE.fullmatch(old_digest)
        or type(old_size) is not int
        or old_size < 0
    ):
        raise HuggingFaceArtifactError(
            "publication journal old digest is invalid"
        )
    return {
        **value,
        "target": target,
        "staged": staged,
        "backup": backup,
    }


def _journal_member(parent: Path, value: object, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise HuggingFaceArtifactError(
            f"publication journal {label} path is invalid"
        )
    return parent / value


def _metadata_for(
    dataset: str,
    revision: str,
    request: ArtifactRequest,
) -> dict[str, object]:
    return {
        "schema": SIDECAR_SCHEMA,
        "dataset": dataset,
        "revision": revision,
        "file_url": build_file_url(
            dataset, revision, request.remote_path
        ),
        "sha256": request.expected_sha256,
        "size": request.expected_size,
    }


def _load_metadata(path: Path) -> dict[str, object]:
    value = _load_json_object(path, label="metadata sidecar")
    if set(value) != SIDECAR_KEYS:
        raise HuggingFaceArtifactError(
            f"metadata sidecar has unexpected keys: {path}; "
            f"expected {sorted(SIDECAR_KEYS)}"
        )
    if value.get("schema") != SIDECAR_SCHEMA:
        raise HuggingFaceArtifactError(
            f"unsupported metadata sidecar schema: {path}"
        )
    sha256 = value.get("sha256")
    size = value.get("size")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise HuggingFaceArtifactError(
            f"invalid SHA-256 in metadata sidecar: {path}"
        )
    if type(size) is not int or size < 0:
        raise HuggingFaceArtifactError(
            f"invalid size in metadata sidecar: {path}"
        )
    return value


def _load_json_object(path: Path, *, label: str) -> dict[str, object]:
    _require_regular_file(path, label=label)
    if path.stat().st_size > 256 * 1024:
        raise HuggingFaceArtifactError(
            f"{label} is unexpectedly large: {path}"
        )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except HuggingFaceArtifactError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HuggingFaceArtifactError(
            f"cannot parse {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise HuggingFaceArtifactError(
            f"{label} must contain a JSON object: {path}"
        )
    return value


def _reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HuggingFaceArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_dataset(dataset: str) -> None:
    if not isinstance(dataset, str) or not DATASET_RE.fullmatch(dataset):
        raise HuggingFaceArtifactError(
            "dataset must have the exact 'owner/name' form using safe URL "
            "characters"
        )


def _validate_request(request: ArtifactRequest) -> None:
    if (
        not isinstance(request.expected_sha256, str)
        or not SHA256_RE.fullmatch(request.expected_sha256)
    ):
        raise HuggingFaceArtifactError(
            f"expected SHA-256 is invalid for {request.remote_path}"
        )
    if type(request.expected_size) is not int or request.expected_size < 0:
        raise HuggingFaceArtifactError(
            f"expected size is invalid for {request.remote_path}"
        )


def _dataset_token(dataset: str) -> str:
    readable = dataset.replace("/", "--")
    digest = hashlib.sha256(dataset.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


def _state_paths(parent: Path, dataset: str) -> tuple[Path, Path]:
    token = _dataset_token(dataset)
    return (
        parent / f".hf-{token}.lock",
        parent / f".hf-{token}.journal.json",
    )


def _acquisition_lock_path(parent: Path, dataset: str) -> Path:
    return parent / f".hf-{_dataset_token(dataset)}.acquire.lock"


@contextlib.contextmanager
def _acquisition_lock(parent: Path, dataset: str) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    fd = _open_lock(_acquisition_lock_path(parent, dataset))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _exclusive_publication_lock(
    parent: Path,
    dataset: str,
) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = _state_paths(parent, dataset)[0]
    fd = _open_lock(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _reader_lock(parent: Path, dataset: str) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    lock_path, journal_path = _state_paths(parent, dataset)
    fd = _open_lock(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if _path_exists(journal_path):
            # A journal means a publisher died during the short commit phase.
            # Temporarily upgrade to repair it, then downgrade for the read.
            fcntl.flock(fd, fcntl.LOCK_UN)
            fcntl.flock(fd, fcntl.LOCK_EX)
            _recover_publication(parent, dataset)
            fcntl.flock(fd, fcntl.LOCK_SH)

        # A writer killed before installing its journal can leave staging and
        # backup files. Clean them only if no live writer owns acquisition;
        # never wait here, since a live writer may be staging a multi-GB file.
        acquisition_fd = _open_lock(
            _acquisition_lock_path(parent, dataset)
        )
        try:
            try:
                fcntl.flock(
                    acquisition_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                pass
            else:
                _cleanup_orphan_work(parent, dataset)
                fcntl.flock(acquisition_fd, fcntl.LOCK_UN)
        finally:
            os.close(acquisition_fd)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HuggingFaceArtifactError(
            f"cannot open dataset lock {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HuggingFaceArtifactError(
            f"dataset lock must be a regular file: {path}"
        )
    return fd


def _write_journal(
    path: Path,
    value: dict[str, object],
    dataset: str,
) -> None:
    temp = _temporary_path(path, "journal", dataset)
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        _unlink_if_exists(temp)


def _temporary_path(
    destination: Path,
    role: str,
    dataset: str,
) -> Path:
    token = _dataset_token(dataset)
    fd, name = tempfile.mkstemp(
        prefix=f".hf-{token}.{destination.name}.{role}-",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    return Path(name)


def _cleanup_orphan_work(parent: Path, dataset: str) -> None:
    """Remove dataset-scoped transaction files left before a journal existed.

    The caller must hold the dataset's exclusive acquisition lock. A durable
    journal owns every temporary path once publication begins; this cleanup is
    therefore only used after journal recovery (or when no journal was ever
    installed).
    """
    token = _dataset_token(dataset)
    removed = False
    for path in parent.glob(f".hf-{token}.*.tmp"):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HuggingFaceArtifactError(
                f"cannot inspect orphaned transaction file {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(mode):
            raise HuggingFaceArtifactError(
                "orphaned transaction path must be a regular file "
                f"(symlinks rejected): {path}"
            )
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed = True
    if removed:
        _fsync_directory(parent)


def _hardlink_backup(path: Path, dataset: str) -> Path | None:
    if not _path_exists(path):
        return None
    _require_regular_file(path, label="publication target")
    backup = _temporary_path(path, "backup", dataset)
    backup.unlink()
    os.link(path, backup)
    return backup


def _verify_digest(
    path: Path,
    expected_sha256: object,
    expected_size: object,
    *,
    label: str,
) -> None:
    if (
        not isinstance(expected_sha256, str)
        or not SHA256_RE.fullmatch(expected_sha256)
        or type(expected_size) is not int
        or expected_size < 0
    ):
        raise HuggingFaceArtifactError(
            f"invalid expected digest for {label}: {path}"
        )
    _require_regular_file(path, label=label)
    digest, size = sha256_file(path)
    if digest != expected_sha256 or size != expected_size:
        raise HuggingFaceArtifactError(
            f"{label} digest mismatch: {path}"
        )


def _cleanup_staged(staged: Sequence[_StagedArtifact]) -> None:
    for item in staged:
        if item.data_path != item.request.destination:
            _unlink_if_exists(item.data_path)
        _unlink_if_exists(item.sidecar_path)


def _fsync_file(path: Path) -> None:
    _require_regular_file(path, label="staged download")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        raise HuggingFaceArtifactError(
            f"cannot open cache directory for fsync: {directory}"
        ) from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise HuggingFaceArtifactError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise HuggingFaceArtifactError(
            f"cannot inspect {label} {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise HuggingFaceArtifactError(
            f"{label} must be a regular file (symlinks rejected): {path}"
        )


def _require_regular_or_absent(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HuggingFaceArtifactError(
            f"cannot inspect {label} {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise HuggingFaceArtifactError(
            f"{label} must be a regular file (symlinks rejected): {path}"
        )


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
