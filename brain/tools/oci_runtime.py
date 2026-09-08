#!/usr/bin/env python3
"""Offline OCI image and numerical-policy checks for the trusted replay launcher.

An OCI manifest digest identifies one platform manifest, never an image index or
mutable tag. The local engine is a trusted execution service; this verifier does
not purport to attest a compromised engine or arbitrary same-UID host Python code.
"""
from __future__ import annotations

import copy
import ctypes
import gzip
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import execution_environment as environment

POLICY_SCHEMA = "wikilean.oci-runtime-policy/v1"
POLICY_LABEL = "org.wikilean.runtime-policy-sha256"
MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA = "application/vnd.oci.image.layer.v1.tar"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
JSON_LIMIT = 4 * 1024 * 1024
EXPANDED_LAYER_LIMIT = 16 * 1024**3
CORE_TYPES = {"x86_64": "Prescott", "aarch64": "ARMV8"}
BLAS_SYMBOLS = {
    "openblas_get_corename", "openblas_get_corename64_",
    "scipy_openblas_get_corename", "scipy_openblas_get_corename64_",
}


class OCIRuntimeError(ValueError):
    """The supplied image, artifact closure, or numerical policy is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OCIRuntimeError(message)


def _keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys,
             f"{label}: expected exactly {sorted(keys)!r}")
    return value


def _json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            _require(key not in result, f"{label}: duplicate JSON key")
            result[key] = value
        return result
    _require(len(raw) <= JSON_LIMIT, f"{label}: control document exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               OCIRuntimeError(f"{label}: nonfinite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise OCIRuntimeError(f"{label}: invalid JSON: {exc}") from exc
    _require(isinstance(value, dict), f"{label}: expected an object")
    return value


def read_control(path: Path) -> tuple[dict[str, Any], bytes]:
    descriptor = environment._open_regular_file_nofollow(path)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= JSON_LIMIT, "control document exceeds size limit")
        raw = stream.read(JSON_LIMIT + 1)
        after = os.fstat(stream.fileno())
        _require(environment._stable_file_state(before) == environment._stable_file_state(after),
                 "control document changed during read")
    expected, size = environment.secure_file_digest(path)
    _require((hashlib.sha256(raw).hexdigest(), len(raw)) == (expected, size),
             "control document path changed during read")
    return _json(raw, str(path)), raw


def validate_policy(value: Any) -> dict[str, Any]:
    policy = _keys(value, {"schema", "architecture", "python", "numpy", "cpu"}, "policy")
    _require(policy["schema"] == POLICY_SCHEMA, "unknown OCI runtime policy schema")
    architecture = policy["architecture"]
    _require(architecture in CORE_TYPES, "unsupported numerical-policy architecture")
    _require(policy["python"] == "/usr/local/bin/python3.12", "policy requires the fixed CPython 3.12 entry point")
    package = _keys(policy["numpy"], {"version", "wheel", "sha256", "bytes"}, "numpy artifact")
    environment._exact_version(package["version"], "numpy.version")
    _require(isinstance(package["wheel"], str) and re.fullmatch(r"numpy-[A-Za-z0-9_.+-]+\.whl", package["wheel"]) is not None,
             "numpy artifact must be one wheel basename")
    environment._digest(package["sha256"], "numpy.sha256")
    _require(type(package["bytes"]) is int and 0 < package["bytes"] <= 1024**3,
             "numpy artifact has invalid byte count")
    cpu = _keys(policy["cpu"], {"baseline", "disable", "openblas_core", "blas_library", "blas_symbol"}, "cpu policy")
    for key in ("baseline", "disable"):
        values = cpu[key]
        _require(isinstance(values, list) and all(
            isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item)
            for item in values) and values == sorted(set(values)), f"cpu.{key}: expected sorted unique feature names")
    _require(bool(cpu["baseline"]) and not set(cpu["baseline"]) & set(cpu["disable"]),
             "CPU baseline must be nonempty and disjoint from disabled dispatch")
    _require(cpu["openblas_core"] == CORE_TYPES[architecture], "OpenBLAS must use the fixed architecture baseline")
    _require(isinstance(cpu["blas_library"], str) and re.fullmatch(
        r"site-packages/numpy\.libs/[A-Za-z0-9_.+-]+\.so(?:\.[A-Za-z0-9_.+-]+)?", cpu["blas_library"]),
        "BLAS library must be in the verified NumPy companion tree")
    _require(cpu["blas_symbol"] in BLAS_SYMBOLS, "unsupported OpenBLAS identity symbol")
    return policy


def numerical_environment(policy: dict[str, Any]) -> dict[str, str]:
    cpu = validate_policy(policy)["cpu"]
    return {"NPY_DISABLE_CPU_FEATURES": ",".join(cpu["disable"]),
            "OPENBLAS_CORETYPE": cpu["openblas_core"]}


def verify_numerical_runtime(policy: dict[str, Any], *, numpy_module: Any = None) -> None:
    """Verify compiled CPU policy and the actual selected OpenBLAS core.

    This runs only after the launcher's complete environment was installed, before
    reduction. The runner also keeps these settings for every sandboxed child.
    """
    policy = validate_policy(policy)
    _require(all(os.environ.get(key) == value for key, value in numerical_environment(policy).items()),
             "numerical environment does not match the sealed policy")
    if numpy_module is None:
        import numpy as numpy_module
    _require(numpy_module.__version__ == policy["numpy"]["version"], "NumPy version disagrees with policy")
    core = numpy_module._core._multiarray_umath
    cpu = policy["cpu"]
    _require(sorted(core.__cpu_baseline__) == cpu["baseline"], "compiled NumPy baseline disagrees with policy")
    _require(sorted(core.__cpu_dispatch__) == cpu["disable"], "policy does not disable every compiled NumPy dispatch target")
    _require(all(core.__cpu_features__.get(feature) is False for feature in cpu["disable"]),
             "NumPy retains an enabled CPU dispatch target")
    import sysconfig
    library = Path(sysconfig.get_path("platlib")) / Path(cpu["blas_library"]).relative_to("site-packages")
    environment.secure_file_digest(library)
    loaded = ctypes.CDLL(str(library))
    function = getattr(loaded, cpu["blas_symbol"])
    function.restype = ctypes.c_char_p
    function.argtypes = []
    _require(function() == cpu["openblas_core"].encode("ascii"), "OpenBLAS selected a different CPU core")


@dataclass(frozen=True)
class VerifiedImage:
    manifest_digest: str
    config_digest: str
    architecture: str
    diff_ids: tuple[str, ...]
    policy_sha256: str

    def runtime(self) -> dict[str, str]:
        return {"kind": "oci-image", "os": "linux", "architecture": self.architecture,
                "manifest_digest": self.manifest_digest}


def _blob(layout: Path, descriptor: Any, media: str | set[str]) -> Path:
    _keys(descriptor, {"mediaType", "digest", "size"}, "OCI descriptor")
    allowed = {media} if isinstance(media, str) else media
    _require(descriptor["mediaType"] in allowed, "unexpected OCI media type (indexes and external layers are unsupported)")
    environment._hash(descriptor["digest"], "OCI descriptor digest")
    _require(type(descriptor["size"]) is int and 0 < descriptor["size"] <= EXPANDED_LAYER_LIMIT,
             "OCI descriptor size is invalid")
    path = layout / "blobs" / "sha256" / descriptor["digest"][7:]
    _require(environment.secure_file_digest(path) == (descriptor["digest"][7:], descriptor["size"]),
             "OCI blob digest/size mismatch")
    return path


def verify_image(layout: Path, manifest_digest: str, policy: dict[str, Any],
                 artifacts: Path, descriptor: dict[str, Any]) -> VerifiedImage:
    """Verify local raw manifest/config/layers, DiffIDs, and immutable NumPy wheel."""
    policy = validate_policy(policy)
    environment.validate_execution_environment(descriptor)
    _require(descriptor["profile"] == environment.AUTHORITATIVE_OCI_PROFILE,
             "OCI launcher requires an authoritative-oci descriptor")
    environment._hash(manifest_digest, "OCI platform manifest digest")
    manifest_path = layout / "blobs" / "sha256" / manifest_digest[7:]
    manifest, raw = read_control(manifest_path)
    _require(hashlib.sha256(raw).hexdigest() == manifest_digest[7:], "platform manifest digest mismatch")
    _require(manifest.get("schemaVersion") == 2 and manifest.get("mediaType") == MANIFEST_MEDIA,
             "expected an OCI platform manifest, not an image index")
    _require(set(manifest) <= {"schemaVersion", "mediaType", "config", "layers", "annotations"},
             "unsupported OCI manifest fields")
    config_path = _blob(layout, manifest.get("config"), CONFIG_MEDIA)
    config, config_raw = read_control(config_path)
    _require(hashlib.sha256(config_raw).hexdigest() == manifest["config"]["digest"][7:],
             "OCI config changed after descriptor verification")
    architecture = ARCHITECTURES.get(config.get("architecture"))
    _require(config.get("os") == "linux" and architecture == policy["architecture"],
             "OCI image platform disagrees with numerical policy")
    _require(not config.get("variant"), "OCI architecture variants are not supported")
    rootfs = _keys(config.get("rootfs"), {"type", "diff_ids"}, "OCI rootfs")
    _require(rootfs["type"] == "layers" and isinstance(rootfs["diff_ids"], list), "invalid OCI rootfs")
    layers = manifest.get("layers")
    _require(isinstance(layers, list) and 0 < len(layers) <= 128 and len(layers) == len(rootfs["diff_ids"]),
             "OCI layers do not match config DiffIDs")
    for layer, diff_id in zip(layers, rootfs["diff_ids"], strict=True):
        environment._hash(diff_id, "OCI layer DiffID")
        path = _blob(layout, layer, {LAYER_MEDIA, LAYER_MEDIA + "+gzip"})
        before = environment.secure_file_digest(path)
        _require(before == (layer["digest"][7:], layer["size"]), "OCI layer changed after descriptor verification")
        fd = environment._open_regular_file_nofollow(path)
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "rb") as stream:
            reader = gzip.GzipFile(fileobj=stream) if layer["mediaType"].endswith("+gzip") else stream
            try:
                while chunk := reader.read(1024 * 1024):
                    count += len(chunk)
                    _require(count <= EXPANDED_LAYER_LIMIT, "expanded OCI layer exceeds limit")
                    digest.update(chunk)
            except (OSError, EOFError) as exc:
                raise OCIRuntimeError(f"invalid compressed OCI layer: {exc}") from exc
        _require("sha256:" + digest.hexdigest() == diff_id, "OCI layer DiffID mismatch")
        _require(environment.secure_file_digest(path) == before, "OCI layer changed during verification")
    image_config = config.get("config")
    _require(isinstance(image_config, dict), "missing OCI execution config")
    _require(not image_config.get("Volumes"), "OCI images with implicit volumes are forbidden")
    labels = image_config.get("Labels") or {}
    policy_sha256 = hashlib.sha256(environment.canonical_json_bytes(policy)).hexdigest()
    _require(isinstance(labels, dict) and labels.get(POLICY_LABEL) == policy_sha256,
             "image does not bind the reviewed numerical/artifact policy")
    package = policy["numpy"]
    _require(environment.secure_file_digest(artifacts / package["wheel"]) == (package["sha256"], package["bytes"]),
             "immutable NumPy wheel digest/size mismatch")
    lock = descriptor["dependency_lock"]["packages"]
    _require(len(lock) == 1 and lock[0]["name"] == "numpy" and
             lock[0]["version"] == package["version"] and
             lock[0]["locked_artifact_sha256"] == package["sha256"],
             "descriptor dependency lock disagrees with immutable wheel")
    image = VerifiedImage(manifest_digest, manifest["config"]["digest"], architecture,
                          tuple(rootfs["diff_ids"]), policy_sha256)
    _require(image.runtime() == descriptor["runtime"], "image disagrees with sealed runtime identity")
    return image
