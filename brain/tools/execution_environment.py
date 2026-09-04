#!/usr/bin/env python3
"""Strict identity contract for an offline Brain replay environment.

The descriptor intentionally contains no absolute paths, timestamps, hostnames,
or mutable image tags.  ``development-host`` records are useful for local replay
and diagnostics but are explicitly not authoritative.  Only the
``authoritative-oci`` profile names a release-grade runtime boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any


MAX_SAFE_INTEGER = 9_007_199_254_740_991
EXECUTION_ENVIRONMENT_SCHEMA = "wikilean.execution-environment/v1"
EXECUTION_ENVIRONMENT_DOMAIN = "wikilean.execution-environment.v1"
DEPENDENCY_LOCK_SCHEMA = "wikilean.python-dependency-lock/v1"
DEVELOPMENT_HOST_PROFILE = "development-host"
AUTHORITATIVE_OCI_PROFILE = "authoritative-oci"

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
PYTHON_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
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
    prefix = (
        f"wikilean\0{EXECUTION_ENVIRONMENT_DOMAIN}\0canonical-json-v1\0"
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + canonical_json_bytes(value)).hexdigest()


def _validate_runtime(value: Any, profile: str) -> tuple[str, str]:
    location = "$.runtime"
    if profile == DEVELOPMENT_HOST_PROFILE:
        runtime = _object(
            value,
            location,
            {"kind", "os", "architecture", "runtime_root"},
        )
        if runtime["kind"] != "development-host":
            _fail(f"{location}.kind", "development-host profile requires kind='development-host'")
        _hash(runtime["runtime_root"], f"{location}.runtime_root")
    else:
        runtime = _object(
            value,
            location,
            {"kind", "os", "architecture", "image_digest"},
        )
        if runtime["kind"] != "oci-image":
            _fail(f"{location}.kind", "authoritative-oci profile requires kind='oci-image'")
        if runtime["os"] != "linux":
            _fail(f"{location}.os", "authoritative OCI replay currently requires Linux")
        _hash(runtime["image_digest"], f"{location}.image_digest")
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
        {"implementation", "version", "cache_tag", "soabi", "executable_sha256"},
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
    _digest(python["executable_sha256"], f"{location}.executable_sha256")


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
            {"name", "version", "artifact_sha256", "installed_files_root"},
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
        _digest(package["artifact_sha256"], f"{package_location}.artifact_sha256")
        _hash(package["installed_files_root"], f"{package_location}.installed_files_root")
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
        {"version", "source_id", "binary_sha256", "compile_options"},
    )
    _exact_version(sqlite["version"], f"{location}.version")
    _printable_ascii(sqlite["source_id"], f"{location}.source_id", max_length=512)
    _digest(sqlite["binary_sha256"], f"{location}.binary_sha256")
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
    if type(locale["utf8_mode"]) is not int or locale["utf8_mode"] not in {0, 1}:
        _fail(f"{location}.utf8_mode", "expected integer 0 or 1")


def _validate_sandbox(value: Any, profile: str, operating_system: str) -> None:
    location = "$.sandbox"
    sandbox = _object(
        value,
        location,
        {
            "backend",
            "version",
            "executable_sha256",
            "policy_id",
            "policy_sha256",
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
    _exact_version(sandbox["version"], f"{location}.version")
    _digest(sandbox["executable_sha256"], f"{location}.executable_sha256")
    _pattern(
        sandbox["policy_id"],
        f"{location}.policy_id",
        NAME_RE,
        "a lowercase versioned policy ID",
    )
    _digest(sandbox["policy_sha256"], f"{location}.policy_sha256")
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
