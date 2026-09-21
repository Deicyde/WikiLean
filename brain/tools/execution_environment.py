#!/usr/bin/env python3
"""Strict identity contract for an offline Brain replay environment.

The descriptor intentionally contains no absolute paths, timestamps, hostnames,
or mutable image tags.  ``development-host`` records are useful for local replay
and diagnostics but are explicitly not authoritative.  Only the
``authoritative-oci`` profile names a release-grade runtime boundary.
"""
from __future__ import annotations

import codecs
import copy
import hashlib
import json
import locale as locale_module
import os
import platform
import re
import stat
import sys
import sysconfig
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any


MAX_SAFE_INTEGER = 9_007_199_254_740_991
EXECUTION_ENVIRONMENT_SCHEMA = "wikilean.execution-environment/v1"
EXECUTION_ENVIRONMENT_DOMAIN = "wikilean.execution-environment.v1"
LIVE_PROBE_SCHEMA = "wikilean.execution-environment-probe/v1"
TRUSTED_RUNTIME_EVIDENCE_SCHEMA = "wikilean.trusted-runtime-evidence/v1"
DEPENDENCY_LOCK_SCHEMA = "wikilean.python-dependency-lock/v1"
DEVELOPMENT_HOST_PROFILE = "development-host"
AUTHORITATIVE_OCI_PROFILE = "authoritative-oci"
RUNNER_FILES_DOMAIN = "wikilean.replay-runner-files.v1"
NUMPY_INSTALLED_TREE_DOMAIN = "wikilean.numpy-installed-tree.v1"
DEVELOPMENT_HOST_DOMAIN = "wikilean.development-host.v1"
SANDBOX_POLICY_DOMAIN = "wikilean.replay-sandbox-policy.v1"
HASH_SENTINEL_TEXT = "wikilean-replay-hash-sentinel-v1"
FILE_READ_CHUNK_SIZE = 1024 * 1024

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
PYTHON_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
SIGNED_INTEGER_RE = re.compile(r"^(?:0|-?[1-9][0-9]{0,31})$")
# Exact labels may use PEP 440 epoch/local syntax, but never ranges, wildcards,
# branch names, or whitespace.
EXACT_VERSION_RE = re.compile(
    r"^[0-9](?:[0-9A-Za-z._+!-]{0,126}[0-9A-Za-z])?$"
)
FLOATING_VERSION_WORDS = frozenset(
    {"head", "latest", "main", "master", "stable", "x"}
)


class ExecutionEnvironmentError(ValueError):
    """An execution-environment document violates the v1 contract."""


def _fail(location: str, message: str) -> None:
    raise ExecutionEnvironmentError(f"{location}: {message}")


def _string(
    value: Any,
    location: str,
    *,
    max_length: int = 512,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str):
        _fail(location, "expected a string")
    if nonempty and not value:
        _fail(location, "must not be empty")
    if len(value) > max_length:
        _fail(location, f"must contain at most {max_length} characters")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail(location, "must contain Unicode scalar values, not surrogates")
    if unicodedata.normalize("NFC", value) != value:
        _fail(location, "must already be Unicode NFC")
    return value


def _object(
    value: Any,
    location: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "expected an object")
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        _fail(location, "missing required keys: " + ", ".join(missing))
    if unknown:
        _fail(location, "unknown keys: " + ", ".join(unknown))
    return value


def _pattern(
    value: Any,
    location: str,
    pattern: re.Pattern[str],
    description: str,
) -> str:
    text = _string(value, location, max_length=128)
    if pattern.fullmatch(text) is None:
        _fail(location, f"expected {description}")
    return text


def _hash(value: Any, location: str) -> str:
    return _pattern(value, location, HASH_RE, "a lowercase sha256:<64-hex> root")


def _digest(value: Any, location: str) -> str:
    return _pattern(value, location, DIGEST_RE, "a lowercase 64-hex SHA-256 digest")


def _exact_version(value: Any, location: str) -> str:
    version = _pattern(
        value,
        location,
        EXACT_VERSION_RE,
        "an exact version label, not a range or wildcard",
    )
    version_words = re.split(r"[.!+_-]", version.casefold())
    if any(word in FLOATING_VERSION_WORDS for word in version_words):
        _fail(location, "expected an exact version label, not a floating label")
    return version


def _printable_ascii(value: Any, location: str, *, max_length: int) -> str:
    text = _string(value, location, max_length=max_length)
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in text):
        _fail(location, "must contain printable ASCII only")
    return text


def _canonical_json_value(value: Any, location: str = "$") -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _fail(location, "integer exceeds the portable JSON range")
        return value
    if isinstance(value, float):
        _fail(location, "floating-point JSON numbers are forbidden")
    if isinstance(value, str):
        return _string(value, location, nonempty=False, max_length=4096)
    if isinstance(value, list):
        return [
            _canonical_json_value(item, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _string(
                key, f"{location}.<key>", nonempty=False, max_length=256
            )
            result[normalized_key] = _canonical_json_value(
                item, f"{location}.{normalized_key}"
            )
        return result
    _fail(location, f"unsupported canonical JSON type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical-json-v1 bytes used by the environment identity."""
    return json.dumps(
        _canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _domain_hash(domain: str, value: Any) -> str:
    prefix = f"wikilean\0{domain}\0canonical-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + canonical_json_bytes(value)).hexdigest()


def _stable_file_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_regular_file_nofollow(path: str | os.PathLike[str]) -> int:
    """Open an absolute path without following any symlink component."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ExecutionEnvironmentError(
            "secure file hashing requires POSIX openat and O_NOFOLLOW support"
        )
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise ExecutionEnvironmentError("physical file paths must be text paths")
    absolute = os.path.normpath(os.path.abspath(raw_path))
    components = [component for component in absolute.split(os.sep) if component]
    if not components:
        raise ExecutionEnvironmentError(f"expected a regular file, got {absolute!r}")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | os.O_NOFOLLOW
    )
    directory_fd = os.open(os.sep, directory_flags)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(components[-1], file_flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ExecutionEnvironmentError(
            f"cannot securely open regular file {absolute!r}: {exc}"
        ) from exc
    finally:
        os.close(directory_fd)

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ExecutionEnvironmentError(
                f"expected a regular file, got {absolute!r}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def secure_file_digest(
    path: str | os.PathLike[str],
) -> tuple[str, int]:
    """Return ``(sha256, byte_length)`` for one stable, symlink-free file.

    Every path component is opened with ``O_NOFOLLOW``. Metadata is checked
    before and after the streaming read, and the path is reopened afterward to
    ensure that it still names the same file. Timestamps and physical paths do
    not enter the returned identity.
    """
    first_fd = _open_regular_file_nofollow(path)
    try:
        opened = os.fstat(first_fd)
        if opened.st_size < 0 or opened.st_size > MAX_SAFE_INTEGER:
            raise ExecutionEnvironmentError(
                "file byte length exceeds the portable JSON integer range"
            )
        digest = hashlib.sha256()
        byte_length = 0
        while chunk := os.read(first_fd, FILE_READ_CHUNK_SIZE):
            digest.update(chunk)
            byte_length += len(chunk)
        finished = os.fstat(first_fd)
        if (
            not os.path.samestat(opened, finished)
            or _stable_file_state(opened) != _stable_file_state(finished)
            or byte_length != opened.st_size
        ):
            raise ExecutionEnvironmentError("file changed while being hashed")
    finally:
        os.close(first_fd)

    second_fd = _open_regular_file_nofollow(path)
    try:
        reopened = os.fstat(second_fd)
        if (
            not os.path.samestat(finished, reopened)
            or _stable_file_state(finished) != _stable_file_state(reopened)
        ):
            raise ExecutionEnvironmentError("file path changed while being hashed")
    finally:
        os.close(second_fd)
    return digest.hexdigest(), byte_length


def _logical_file_path(value: Any, location: str) -> str:
    path = _string(value, location, max_length=4096)
    if "\\" in path or "\x00" in path:
        _fail(location, "must be a portable POSIX logical path")
    pure = PurePosixPath(path)
    parts = path.split("/")
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        _fail(location, "must be a normalized relative POSIX logical path")
    if any(
        any(ord(char) < 0x20 or ord(char) == 0x7F for char in part)
        for part in parts
    ):
        _fail(location, "must not contain control characters")
    return path


def _file_digest_result(result: Any, location: str) -> tuple[str, int]:
    if not isinstance(result, tuple) or len(result) != 2:
        _fail(location, "file digest helper must return (sha256, byte_length)")
    digest, byte_length = result
    normalized_digest = _digest(digest, f"{location}.sha256")
    if (
        type(byte_length) is not int
        or byte_length < 0
        or byte_length > MAX_SAFE_INTEGER
    ):
        _fail(
            f"{location}.byte_length",
            "expected a non-negative portable JSON integer",
        )
    return normalized_digest, byte_length


def logical_file_set_entries(
    files: Mapping[str, str | os.PathLike[str]],
    *,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> list[dict[str, Any]]:
    """Hash an explicit logical-to-physical file mapping.

    Logical paths, byte lengths, and file bytes define the result. Physical
    locations, mapping insertion order, modes, ownership, and timestamps do
    not. Portable case-fold aliases and file/directory ancestry collisions are
    rejected so the same set cannot unpack differently on supported hosts.
    """
    if not isinstance(files, Mapping) or not files:
        raise ExecutionEnvironmentError("logical file set must be a non-empty mapping")

    normalized: list[tuple[str, str | os.PathLike[str]]] = []
    aliases: dict[tuple[str, ...], str] = {}
    for index, (logical_path, physical_path) in enumerate(files.items()):
        logical = _logical_file_path(logical_path, f"$.files[{index}].path")
        alias = tuple(part.casefold() for part in logical.split("/"))
        prior = aliases.get(alias)
        if prior is not None:
            _fail(
                f"$.files[{index}].path",
                f"portable path alias collides with {prior!r}",
            )
        aliases[alias] = logical
        normalized.append((logical, physical_path))

    alias_set = set(aliases)
    for alias, logical in aliases.items():
        if any(alias[:length] in alias_set for length in range(1, len(alias))):
            _fail(
                "$.files",
                f"logical file path has a file ancestor: {logical!r}",
            )

    entries: list[dict[str, Any]] = []
    for logical, physical in sorted(normalized, key=lambda item: item[0]):
        digest, byte_length = _file_digest_result(
            file_digest(physical), f"$.files[{logical!r}]"
        )
        entries.append(
            {"path": logical, "bytes": byte_length, "sha256": digest}
        )
    return entries


def logical_file_set_root(
    files: Mapping[str, str | os.PathLike[str]],
    *,
    domain: str,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> str:
    """Return a domain-separated root for a relocation-independent file set."""
    if domain not in {RUNNER_FILES_DOMAIN, NUMPY_INSTALLED_TREE_DOMAIN}:
        raise ExecutionEnvironmentError("unrecognized logical file-set domain")
    entries = logical_file_set_entries(files, file_digest=file_digest)
    return _domain_hash(domain, {"files": entries})


def runner_files_root(
    files: Mapping[str, str | os.PathLike[str]],
    *,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> str:
    """Hash the exact logical reducer/runner file closure."""
    return logical_file_set_root(
        files, domain=RUNNER_FILES_DOMAIN, file_digest=file_digest
    )


def numpy_installed_tree_root(
    files: Mapping[str, str | os.PathLike[str]],
    *,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> str:
    """Hash an explicitly enumerated installed NumPy runtime closure."""
    return logical_file_set_root(
        files, domain=NUMPY_INSTALLED_TREE_DOMAIN, file_digest=file_digest
    )


def development_host_fingerprint(
    *,
    operating_system: str | None = None,
    architecture: str | None = None,
    kernel_release: str | None = None,
    libc_name: str | None = None,
    libc_version: str | None = None,
) -> str:
    """Hash stable host runtime facts without recording host paths or names."""
    detected_libc_name, detected_libc_version = platform.libc_ver()
    facts = {
        "os": operating_system if operating_system is not None else platform.system().lower(),
        "architecture": architecture if architecture is not None else platform.machine().lower(),
        "kernel_release": kernel_release if kernel_release is not None else platform.release(),
        "libc": {
            "name": libc_name if libc_name is not None else detected_libc_name,
            "version": libc_version if libc_version is not None else detected_libc_version,
        },
    }
    _pattern(
        facts["os"],
        "$.host.os",
        TOKEN_RE,
        "a lowercase operating-system token",
    )
    _pattern(
        facts["architecture"],
        "$.host.architecture",
        TOKEN_RE,
        "a lowercase architecture token",
    )
    _printable_ascii(
        facts["kernel_release"], "$.host.kernel_release", max_length=512
    )
    for key in ("name", "version"):
        value = _string(
            facts["libc"][key],
            f"$.host.libc.{key}",
            max_length=512,
            nonempty=False,
        )
        if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
            _fail(f"$.host.libc.{key}", "must contain printable ASCII only")
    return _domain_hash(DEVELOPMENT_HOST_DOMAIN, facts)


def sandbox_policy_root(policy: Any) -> str:
    """Hash a relocation-independent, structural sandbox policy description."""
    return _domain_hash(SANDBOX_POLICY_DOMAIN, policy)


def probe_python_runtime(
    *,
    implementation: str | None = None,
    version: str | None = None,
    cache_tag: str | None = None,
    soabi: str | None = None,
    executable_path: str | os.PathLike[str] | None = None,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> dict[str, Any]:
    """Project Python facts observable by the exact probing interpreter."""
    executable = sys.executable if executable_path is None else executable_path
    executable_digest, _byte_length = _file_digest_result(
        file_digest(executable), "$.python.executable_file"
    )
    result = {
        "implementation": (
            platform.python_implementation()
            if implementation is None
            else implementation
        ),
        "version": platform.python_version() if version is None else version,
        "cache_tag": (
            getattr(sys.implementation, "cache_tag", None)
            if cache_tag is None
            else cache_tag
        ),
        "soabi": sysconfig.get_config_var("SOABI") if soabi is None else soabi,
        "executable_file_sha256": executable_digest,
    }
    _validate_python(result)
    return result


def numpy_runtime_facts(
    *,
    version: str,
    installed_files: Mapping[str, str | os.PathLike[str]],
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> dict[str, Any]:
    """Project NumPy facts from a caller-supplied exhaustive installed closure.

    Automatic distribution discovery is deliberately excluded: the eventual
    in-sandbox probe must enumerate every importable NumPy package, extension,
    data, and distribution-metadata file under stable logical paths.
    """
    result = {
        "name": "numpy",
        "version": version,
        "installed_tree_root": numpy_installed_tree_root(
            installed_files, file_digest=file_digest
        ),
    }
    _exact_version(result["version"], "$.numpy.version")
    return result


def _numpy_import_roots(scheme_paths: Mapping[str, str]) -> tuple[Path, ...]:
    """Return the explicit purelib/platlib anchors used by the probing parent."""
    if not isinstance(scheme_paths, Mapping):
        _fail("$.numpy.scheme_paths", "expected an object")
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        raw_root = scheme_paths.get(key)
        if not isinstance(raw_root, str) or not raw_root:
            _fail(f"$.numpy.scheme_paths.{key}", "expected an absolute directory path")
        if not os.path.isabs(raw_root) or os.path.normpath(raw_root) != raw_root:
            _fail(
                f"$.numpy.scheme_paths.{key}",
                "expected a normalized absolute directory path",
            )
        root = Path(raw_root)
        try:
            metadata = root.lstat()
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ExecutionEnvironmentError(
                f"$.numpy.scheme_paths.{key}: directory is unavailable: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail(f"$.numpy.scheme_paths.{key}", "expected a real directory")
        if resolved != root:
            _fail(
                f"$.numpy.scheme_paths.{key}",
                "directory path must contain no symlink components",
            )
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _installed_import_location(
    physical_path: Path,
    import_roots: tuple[Path, ...],
) -> tuple[str, Path, PurePosixPath] | None:
    """Map one physical import file to a relocation-independent logical path."""
    absolute = Path(os.path.abspath(os.fspath(physical_path)))
    for root in sorted(import_roots, key=lambda item: len(item.parts), reverse=True):
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            _fail("$.numpy.files", "distribution entry resolves to an import root")
        pure_relative = PurePosixPath(relative.as_posix())
        logical = _logical_file_path(
            f"site-packages/{pure_relative.as_posix()}", "$.numpy.files.path"
        )
        return logical, root, pure_relative
    return None


def _real_numpy_tree(root: Path, location: str) -> None:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ExecutionEnvironmentError(f"{location}: tree is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail(location, "tree must be a real directory")


def _is_numpy_dist_info(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith("numpy-") and folded.endswith(".dist-info")


def _scan_regular_tree(root: Path) -> list[Path]:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ExecutionEnvironmentError(
            f"NumPy installed tree is unavailable: {root}: {exc}"
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ExecutionEnvironmentError(
            f"NumPy installed tree root is not a real directory: {root}"
        )
    files: list[Path] = []
    def traversal_error(exc: OSError) -> None:
        raise ExecutionEnvironmentError(
            f"cannot traverse NumPy installed tree {root}: {exc}"
        ) from exc

    try:
        for directory, names, filenames in os.walk(
            root, followlinks=False, onerror=traversal_error
        ):
            directory_path = Path(directory)
            for name in names:
                child = directory_path / name
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise ExecutionEnvironmentError(
                        f"NumPy installed tree contains a non-directory: {child}"
                    )
            for name in filenames:
                child = directory_path / name
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    raise ExecutionEnvironmentError(
                        f"NumPy installed tree contains a non-regular file: {child}"
                    )
                files.append(child)
    except ExecutionEnvironmentError:
        raise
    except OSError as exc:
        raise ExecutionEnvironmentError(
            f"cannot traverse NumPy installed tree {root}: {exc}"
        ) from exc
    return files


def discover_numpy_installed_files(
    *,
    distribution: Any,
    numpy_module: Any,
    scheme_paths: Mapping[str, str],
) -> dict[str, Path]:
    """Return the complete import/runtime NumPy closure under logical paths.

    The distribution inventory selects the NumPy package and its matching
    dist-info tree. Companion ``numpy.libs``/``numpy.dylibs`` trees are selected
    from the same import roots. Every selected tree is scanned exhaustively so
    unrecorded importable files cannot evade the root. Console scripts, headers,
    and other files outside purelib/platlib are deliberately excluded: they are
    neither imported by the reducer nor exposed inside the replay sandbox.
    """
    distribution_name = getattr(distribution, "metadata", {}).get("Name")
    normalized_name = re.sub(r"[-_.]+", "-", str(distribution_name).casefold())
    if normalized_name != "numpy":
        _fail("$.numpy.name", "installed distribution metadata must name numpy")
    record_files = getattr(distribution, "files", None)
    if not isinstance(record_files, (list, tuple)) or not record_files:
        _fail("$.numpy.files", "installed numpy distribution has no file inventory")

    import_roots = _numpy_import_roots(scheme_paths)
    recorded_import_files: set[Path] = set()
    package_roots: set[Path] = set()
    dist_info_roots: set[Path] = set()
    for index, record_path in enumerate(record_files):
        try:
            located = distribution.locate_file(record_path)
            physical = Path(os.path.normpath(os.path.abspath(os.fspath(located))))
        except (OSError, TypeError, ValueError) as exc:
            raise ExecutionEnvironmentError(
                f"$.numpy.files[{index}]: cannot locate distribution entry: {exc}"
            ) from exc
        location = _installed_import_location(physical, import_roots)
        if location is None:
            continue
        _logical, anchor, relative = location
        recorded_import_files.add(physical)
        top_level = relative.parts[0]
        top_level_path = anchor / top_level
        if top_level.casefold() == "numpy":
            package_roots.add(top_level_path)
        elif _is_numpy_dist_info(top_level):
            dist_info_roots.add(top_level_path)

    module_file = getattr(numpy_module, "__file__", None)
    if not isinstance(module_file, (str, os.PathLike)):
        _fail("$.numpy.module", "imported numpy module has no file path")
    normalized_module_file = Path(
        os.path.normpath(os.path.abspath(os.fspath(module_file)))
    )
    module_location = _installed_import_location(normalized_module_file, import_roots)
    if module_location is None or module_location[2].parts[0].casefold() != "numpy":
        _fail(
            "$.numpy.module",
            "imported numpy module is outside the declared import roots",
        )
    if normalized_module_file not in recorded_import_files:
        _fail(
            "$.numpy.module",
            "imported numpy module is absent from its distribution inventory",
        )

    if not package_roots:
        _fail("$.numpy.files", "distribution inventory has no numpy package tree")
    if len(dist_info_roots) != 1:
        _fail(
            "$.numpy.files",
            "distribution inventory must select exactly one numpy dist-info tree",
        )

    selected_roots = package_roots | dist_info_roots
    for anchor in import_roots:
        for name in ("numpy.libs", "numpy.dylibs"):
            candidate = anchor / name
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ExecutionEnvironmentError(
                    f"$.numpy.files: companion runtime tree is unavailable: {exc}"
                ) from exc
            _real_numpy_tree(candidate, "$.numpy.files")
            selected_roots.add(candidate)

    installed_files: set[Path] = set()
    for root in sorted(selected_roots):
        _real_numpy_tree(root, "$.numpy.files")
        installed_files.update(_scan_regular_tree(root))

    result: dict[str, Path] = {}
    for physical in sorted(installed_files):
        location = _installed_import_location(physical, import_roots)
        if location is None:
            _fail("$.numpy.files", "selected runtime file escapes the import roots")
        logical = location[0]
        if logical in result and result[logical] != physical:
            _fail("$.numpy.files", f"duplicate installed logical path {logical!r}")
        result[logical] = physical
    return result


def probe_numpy_runtime(
    *,
    distribution: Any = None,
    numpy_module: Any = None,
    scheme_paths: Mapping[str, str] | None = None,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> dict[str, Any]:
    """Probe NumPy version and its exhaustive installed file closure."""
    if distribution is None:
        from importlib import metadata

        distribution = metadata.distribution("numpy")
    if numpy_module is None:
        import importlib

        numpy_module = importlib.import_module("numpy")
    paths = dict(sysconfig.get_paths() if scheme_paths is None else scheme_paths)
    distribution_version = getattr(distribution, "version", None)
    module_version = getattr(numpy_module, "__version__", None)
    if distribution_version != module_version:
        _fail(
            "$.numpy.version",
            "imported numpy version disagrees with distribution metadata",
        )
    first_files = discover_numpy_installed_files(
        distribution=distribution,
        numpy_module=numpy_module,
        scheme_paths=paths,
    )
    result = numpy_runtime_facts(
        version=distribution_version,
        installed_files=first_files,
        file_digest=file_digest,
    )
    second_files = discover_numpy_installed_files(
        distribution=distribution,
        numpy_module=numpy_module,
        scheme_paths=paths,
    )
    second_root = numpy_installed_tree_root(
        second_files, file_digest=file_digest
    )
    final_files = discover_numpy_installed_files(
        distribution=distribution,
        numpy_module=numpy_module,
        scheme_paths=paths,
    )
    if (
        first_files != second_files
        or second_files != final_files
        or second_root != result["installed_tree_root"]
    ):
        _fail("$.numpy.files", "installed numpy closure changed while being hashed")
    return result


def _validate_numpy_runtime_facts(value: Any) -> dict[str, Any]:
    numpy = _object(value, "$.numpy", {"name", "version", "installed_tree_root"})
    if numpy["name"] != "numpy":
        _fail("$.numpy.name", "expected 'numpy'")
    _exact_version(numpy["version"], "$.numpy.version")
    _hash(numpy["installed_tree_root"], "$.numpy.installed_tree_root")
    return numpy


def validate_live_probe_document(value: Any) -> dict[str, Any]:
    """Validate the strict document emitted by the in-sandbox probe."""
    probe = _object(value, "$", {"schema", "python", "numpy", "sqlite", "locale"})
    if probe["schema"] != LIVE_PROBE_SCHEMA:
        _fail("$.schema", f"expected {LIVE_PROBE_SCHEMA!r}")
    _validate_python(probe["python"])
    _validate_numpy_runtime_facts(probe["numpy"])
    _validate_sqlite(probe["sqlite"])
    _validate_locale(probe["locale"])
    return probe


def validate_trusted_runtime_evidence(value: Any) -> dict[str, Any]:
    """Validate explicit outer-launcher evidence for authoritative OCI replay."""
    evidence = _object(value, "$", {"schema", "profile", "runtime"})
    if evidence["schema"] != TRUSTED_RUNTIME_EVIDENCE_SCHEMA:
        _fail("$.schema", f"expected {TRUSTED_RUNTIME_EVIDENCE_SCHEMA!r}")
    if evidence["profile"] != AUTHORITATIVE_OCI_PROFILE:
        _fail("$.profile", "trusted runtime evidence must be authoritative-oci")
    _validate_runtime(evidence["runtime"], AUTHORITATIVE_OCI_PROFILE)
    return evidence


def probe_sqlite_runtime(
    *,
    sqlite_module: Any = None,
    extension_module: Any = None,
    connect: Callable[[str], Any] | None = None,
    file_digest: Callable[[str | os.PathLike[str]], tuple[str, int]] = secure_file_digest,
) -> dict[str, Any]:
    """Project SQLite facts from the loaded Python extension and live engine."""
    if sqlite_module is None:
        import sqlite3 as sqlite_module
    if extension_module is None:
        import _sqlite3 as extension_module

    connector = sqlite_module.connect if connect is None else connect
    connection = connector(":memory:")
    try:
        version_row = connection.execute(
            "SELECT sqlite_version(), sqlite_source_id()"
        ).fetchone()
        option_rows = connection.execute("PRAGMA compile_options").fetchall()
    except Exception as exc:
        raise ExecutionEnvironmentError(f"cannot probe live SQLite engine: {exc}") from exc
    finally:
        connection.close()
    if (
        not isinstance(version_row, (tuple, list))
        or len(version_row) != 2
        or not isinstance(version_row[0], str)
        or not isinstance(version_row[1], str)
    ):
        raise ExecutionEnvironmentError("SQLite version/source probe returned an invalid row")
    module_version = getattr(sqlite_module, "sqlite_version", None)
    if module_version != version_row[0]:
        raise ExecutionEnvironmentError(
            "sqlite3.sqlite_version disagrees with the connected SQLite engine"
        )
    options: list[str] = []
    for index, row in enumerate(option_rows):
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 1
            or not isinstance(row[0], str)
        ):
            raise ExecutionEnvironmentError(
                f"SQLite compile option row {index} is invalid"
            )
        options.append(row[0])
    extension_path = getattr(extension_module, "__file__", None)
    if not isinstance(extension_path, (str, os.PathLike)):
        raise ExecutionEnvironmentError("loaded _sqlite3 extension has no file path")
    extension_digest, _byte_length = _file_digest_result(
        file_digest(extension_path), "$.sqlite.extension_file"
    )
    result = {
        "version": version_row[0],
        "source_id": version_row[1],
        "extension_file_sha256": extension_digest,
        "compile_options": sorted(options),
    }
    _validate_sqlite(result)
    return result


def probe_locale_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    preferred_encoding: str | None = None,
    filesystem_encoding: str | None = None,
    utf8_mode: int | None = None,
    hash_sentinel: str | None = None,
) -> dict[str, Any]:
    """Project locale and hash settings observable by the probing process."""
    process_environment = os.environ if environ is None else environ
    observed_preferred = (
        locale_module.getpreferredencoding(False)
        if preferred_encoding is None
        else preferred_encoding
    )
    observed_filesystem = (
        sys.getfilesystemencoding()
        if filesystem_encoding is None
        else filesystem_encoding
    )
    try:
        canonical_preferred = codecs.lookup(observed_preferred).name
        canonical_filesystem = codecs.lookup(observed_filesystem).name
    except (LookupError, TypeError) as exc:
        raise ExecutionEnvironmentError(
            f"cannot normalize observed Python encoding: {exc}"
        ) from exc
    result = {
        "lang": process_environment.get("LANG"),
        "lc_all": process_environment.get("LC_ALL"),
        "timezone": process_environment.get("TZ"),
        "preferred_encoding": canonical_preferred,
        "filesystem_encoding": canonical_filesystem,
        "utf8_mode": sys.flags.utf8_mode if utf8_mode is None else utf8_mode,
        "python_hash_seed": process_environment.get("PYTHONHASHSEED"),
        "hash_sentinel": (
            str(hash(HASH_SENTINEL_TEXT))
            if hash_sentinel is None
            else hash_sentinel
        ),
    }
    _validate_locale(result)
    return result


def live_environment_projection(environment: Any) -> dict[str, Any]:
    """Return the descriptor subset that a sandboxed live probe can compare.

    ``locked_artifact_sha256`` is intentionally absent: an installed tree can
    be measured, but its originating wheel/archive bytes cannot be recovered
    from that tree. OCI ``manifest_digest`` must be supplied by a trusted outer
    launcher rather than inferred from inside a container.
    """
    validated = validate_execution_environment(environment)
    package = validated["dependency_lock"]["packages"][0]
    return {
        "profile": validated["profile"],
        "runtime": copy.deepcopy(validated["runtime"]),
        "runner": {"files_root": validated["runner"]["files_root"]},
        "python": copy.deepcopy(validated["python"]),
        "numpy": {
            "name": package["name"],
            "version": package["version"],
            "installed_tree_root": package["installed_tree_root"],
        },
        "sqlite": copy.deepcopy(validated["sqlite"]),
        "locale": copy.deepcopy(validated["locale"]),
        "sandbox": copy.deepcopy(validated["sandbox"]),
    }


def validate_live_environment_projection(value: Any) -> dict[str, Any]:
    """Validate the strict comparable projection used by the replay gate."""
    projection = _object(
        value,
        "$",
        {"profile", "runtime", "runner", "python", "numpy", "sqlite", "locale", "sandbox"},
    )
    profile = projection["profile"]
    if profile not in {DEVELOPMENT_HOST_PROFILE, AUTHORITATIVE_OCI_PROFILE}:
        _fail("$.profile", "expected 'development-host' or 'authoritative-oci'")
    operating_system, _architecture = _validate_runtime(
        projection["runtime"], profile
    )
    runner = _object(projection["runner"], "$.runner", {"files_root"})
    _hash(runner["files_root"], "$.runner.files_root")
    _validate_python(projection["python"])
    _validate_numpy_runtime_facts(projection["numpy"])
    _validate_sqlite(projection["sqlite"])
    _validate_locale(projection["locale"])
    _validate_sandbox(projection["sandbox"], profile, operating_system)
    return projection


def probe_live_environment_projection(
    *,
    profile: str,
    runtime_probe: Callable[[], dict[str, Any]],
    runner_files_probe: Callable[[], str],
    numpy_probe: Callable[[], dict[str, Any]],
    sandbox_probe: Callable[[], dict[str, Any]],
    python_probe: Callable[[], dict[str, Any]] = probe_python_runtime,
    sqlite_probe: Callable[[], dict[str, Any]] = probe_sqlite_runtime,
    locale_probe: Callable[[], dict[str, Any]] = probe_locale_runtime,
) -> dict[str, Any]:
    """Collect a comparable live projection through injectable probes.

    The caller is responsible for executing this function inside the exact
    selected sandbox. ``runtime_probe`` must obtain OCI manifest identity from
    a trusted outer launcher; it must never infer authority from container-local
    marker files.
    """
    if profile not in {DEVELOPMENT_HOST_PROFILE, AUTHORITATIVE_OCI_PROFILE}:
        _fail("$.profile", "expected 'development-host' or 'authoritative-oci'")
    runtime = copy.deepcopy(runtime_probe())
    operating_system, _architecture = _validate_runtime(runtime, profile)
    files_root = runner_files_probe()
    _hash(files_root, "$.runner.files_root")
    python = copy.deepcopy(python_probe())
    _validate_python(python)
    numpy = copy.deepcopy(numpy_probe())
    _validate_numpy_runtime_facts(numpy)
    sqlite = copy.deepcopy(sqlite_probe())
    _validate_sqlite(sqlite)
    locale = copy.deepcopy(locale_probe())
    _validate_locale(locale)
    sandbox = copy.deepcopy(sandbox_probe())
    _validate_sandbox(sandbox, profile, operating_system)
    result = {
        "profile": profile,
        "runtime": runtime,
        "runner": {"files_root": files_root},
        "python": python,
        "numpy": numpy,
        "sqlite": sqlite,
        "locale": locale,
        "sandbox": sandbox,
    }
    return validate_live_environment_projection(result)


def execution_environment_identity(environment: dict[str, Any]) -> str:
    """Derive the environment ID, excluding only the self-referential ID field."""
    if not isinstance(environment, dict):
        _fail("$", "expected an object")
    if environment.get("schema") != EXECUTION_ENVIRONMENT_SCHEMA:
        _fail(
            "$.schema",
            f"unknown execution-environment schema/version {environment.get('schema')!r}",
        )
    value = copy.deepcopy(environment)
    value.pop("environment_id", None)
    return _domain_hash(EXECUTION_ENVIRONMENT_DOMAIN, value)


def _validate_runtime(value: Any, profile: str) -> tuple[str, str]:
    location = "$.runtime"
    if profile == DEVELOPMENT_HOST_PROFILE:
        runtime = _object(
            value,
            location,
            {"kind", "os", "architecture", "host_fingerprint"},
        )
        if runtime["kind"] != "development-host":
            _fail(f"{location}.kind", "development-host profile requires kind='development-host'")
        _hash(runtime["host_fingerprint"], f"{location}.host_fingerprint")
    else:
        runtime = _object(
            value,
            location,
            {"kind", "os", "architecture", "manifest_digest"},
        )
        if runtime["kind"] != "oci-image":
            _fail(f"{location}.kind", "authoritative-oci profile requires kind='oci-image'")
        if runtime["os"] != "linux":
            _fail(f"{location}.os", "authoritative OCI replay currently requires Linux")
        _hash(runtime["manifest_digest"], f"{location}.manifest_digest")
    operating_system = _pattern(
        runtime["os"], f"{location}.os", TOKEN_RE, "a lowercase operating-system token"
    )
    if operating_system not in {"darwin", "linux"}:
        _fail(f"{location}.os", "supported replay operating systems are darwin and linux")
    architecture = _pattern(
        runtime["architecture"],
        f"{location}.architecture",
        TOKEN_RE,
        "a lowercase architecture token",
    )
    return operating_system, architecture


def _validate_runner(value: Any) -> None:
    location = "$.runner"
    runner = _object(value, location, {"name", "version", "git_commit", "files_root"})
    _pattern(runner["name"], f"{location}.name", NAME_RE, "a lowercase runner name")
    _exact_version(runner["version"], f"{location}.version")
    _pattern(
        runner["git_commit"],
        f"{location}.git_commit",
        GIT_COMMIT_RE,
        "a full lowercase Git commit",
    )
    _hash(runner["files_root"], f"{location}.files_root")


def _validate_python(value: Any) -> None:
    location = "$.python"
    python = _object(
        value,
        location,
        {
            "implementation",
            "version",
            "cache_tag",
            "soabi",
            "executable_file_sha256",
        },
    )
    if python["implementation"] != "CPython":
        _fail(f"{location}.implementation", "v1 supports exactly CPython")
    _exact_version(python["version"], f"{location}.version")
    _pattern(
        python["cache_tag"],
        f"{location}.cache_tag",
        PYTHON_TOKEN_RE,
        "an exact Python cache tag",
    )
    _pattern(
        python["soabi"],
        f"{location}.soabi",
        PYTHON_TOKEN_RE,
        "an exact Python SOABI",
    )
    _digest(
        python["executable_file_sha256"],
        f"{location}.executable_file_sha256",
    )


def _validate_dependency_lock(value: Any) -> None:
    location = "$.dependency_lock"
    lock = _object(value, location, {"schema", "packages"})
    if lock["schema"] != DEPENDENCY_LOCK_SCHEMA:
        _fail(
            f"{location}.schema",
            f"expected {DEPENDENCY_LOCK_SCHEMA!r}",
        )
    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        _fail(f"{location}.packages", "expected a non-empty array")
    names: list[str] = []
    for index, value in enumerate(packages):
        package_location = f"{location}.packages[{index}]"
        package = _object(
            value,
            package_location,
            {
                "name",
                "version",
                "locked_artifact_sha256",
                "installed_tree_root",
            },
        )
        names.append(
            _pattern(
                package["name"],
                f"{package_location}.name",
                NAME_RE,
                "a normalized lowercase distribution name",
            )
        )
        _exact_version(package["version"], f"{package_location}.version")
        _digest(
            package["locked_artifact_sha256"],
            f"{package_location}.locked_artifact_sha256",
        )
        _hash(
            package["installed_tree_root"],
            f"{package_location}.installed_tree_root",
        )
    if names != sorted(names):
        _fail(f"{location}.packages", "entries must be sorted by name")
    if len(names) != len(set(names)):
        _fail(f"{location}.packages", "package names must be unique")
    if names != ["numpy"]:
        _fail(
            f"{location}.packages",
            "v1 must contain exactly the numpy runtime dependency",
        )


def _validate_sqlite(value: Any) -> None:
    location = "$.sqlite"
    sqlite = _object(
        value,
        location,
        {"version", "source_id", "extension_file_sha256", "compile_options"},
    )
    _exact_version(sqlite["version"], f"{location}.version")
    _printable_ascii(sqlite["source_id"], f"{location}.source_id", max_length=512)
    _digest(
        sqlite["extension_file_sha256"],
        f"{location}.extension_file_sha256",
    )
    options = sqlite["compile_options"]
    if not isinstance(options, list) or not options:
        _fail(f"{location}.compile_options", "expected a non-empty array")
    if len(options) > 4096:
        _fail(f"{location}.compile_options", "must contain at most 4096 entries")
    normalized = [
        _printable_ascii(
            option,
            f"{location}.compile_options[{index}]",
            max_length=512,
        )
        for index, option in enumerate(options)
    ]
    if normalized != sorted(normalized):
        _fail(f"{location}.compile_options", "entries must be sorted")
    if len(normalized) != len(set(normalized)):
        _fail(f"{location}.compile_options", "entries must be unique")


def _validate_locale(value: Any) -> None:
    location = "$.locale"
    locale = _object(
        value,
        location,
        {
            "lang",
            "lc_all",
            "timezone",
            "preferred_encoding",
            "filesystem_encoding",
            "utf8_mode",
            "python_hash_seed",
            "hash_sentinel",
        },
    )
    constants = {
        "lang": "C.UTF-8",
        "lc_all": "C.UTF-8",
        "timezone": "UTC",
        "preferred_encoding": "utf-8",
        "filesystem_encoding": "utf-8",
        "python_hash_seed": "0",
    }
    for key, expected in constants.items():
        if locale[key] != expected:
            _fail(f"{location}.{key}", f"expected {expected!r}")
    if type(locale["utf8_mode"]) is not int or locale["utf8_mode"] != 1:
        _fail(f"{location}.utf8_mode", "expected integer 1")
    _pattern(
        locale["hash_sentinel"],
        f"{location}.hash_sentinel",
        SIGNED_INTEGER_RE,
        "a canonical signed integer string",
    )


def _validate_sandbox(value: Any, profile: str, operating_system: str) -> None:
    location = "$.sandbox"
    sandbox = _object(
        value,
        location,
        {
            "backend",
            "reported_version",
            "executable_sha256",
            "policy_id",
            "policy_root",
            "network",
        },
    )
    backend = sandbox["backend"]
    expected_backend = {
        "darwin": "darwin-sandbox-exec",
        "linux": "linux-bubblewrap",
    }[operating_system]
    if backend != expected_backend:
        _fail(
            f"{location}.backend",
            f"{operating_system} runtime requires {expected_backend!r}",
        )
    if profile == AUTHORITATIVE_OCI_PROFILE and backend != "linux-bubblewrap":
        _fail(
            f"{location}.backend",
            "authoritative OCI replay requires linux-bubblewrap",
        )
    reported_version = sandbox["reported_version"]
    if reported_version is None:
        if backend != "darwin-sandbox-exec":
            _fail(
                f"{location}.reported_version",
                "may be null only for darwin-sandbox-exec",
            )
    else:
        _exact_version(reported_version, f"{location}.reported_version")
    _digest(sandbox["executable_sha256"], f"{location}.executable_sha256")
    _pattern(
        sandbox["policy_id"],
        f"{location}.policy_id",
        NAME_RE,
        "a lowercase versioned policy ID",
    )
    _hash(sandbox["policy_root"], f"{location}.policy_root")
    if sandbox["network"] != "disabled":
        _fail(f"{location}.network", "network must be 'disabled'")


def validate_execution_environment(environment: Any) -> dict[str, Any]:
    """Validate and return a strict execution-environment/v1 document."""
    required = {
        "schema",
        "environment_id",
        "profile",
        "runtime",
        "runner",
        "python",
        "dependency_lock",
        "sqlite",
        "locale",
        "sandbox",
    }
    obj = _object(environment, "$", required)
    if obj["schema"] != EXECUTION_ENVIRONMENT_SCHEMA:
        _fail(
            "$.schema",
            f"unknown execution-environment schema/version {obj['schema']!r}",
        )
    _hash(obj["environment_id"], "$.environment_id")
    profile = obj["profile"]
    if profile not in {DEVELOPMENT_HOST_PROFILE, AUTHORITATIVE_OCI_PROFILE}:
        _fail(
            "$.profile",
            "expected 'development-host' or 'authoritative-oci'",
        )
    operating_system, _architecture = _validate_runtime(obj["runtime"], profile)
    _validate_runner(obj["runner"])
    _validate_python(obj["python"])
    _validate_dependency_lock(obj["dependency_lock"])
    _validate_sqlite(obj["sqlite"])
    _validate_locale(obj["locale"])
    _validate_sandbox(obj["sandbox"], profile, operating_system)
    expected = execution_environment_identity(obj)
    if obj["environment_id"] != expected:
        _fail("$.environment_id", f"expected {expected}")
    return obj


def seal_execution_environment(environment: dict[str, Any]) -> dict[str, Any]:
    """Return a validated copy with its self-derived ``environment_id`` filled."""
    if not isinstance(environment, dict):
        _fail("$", "expected an object")
    value = copy.deepcopy(environment)
    value["environment_id"] = execution_environment_identity(value)
    return validate_execution_environment(value)
