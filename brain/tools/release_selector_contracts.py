"""Strict public Brain release-selector contracts.

V2 binds the logical release to the exact manifest bytes and public namespace.
V1 remains readable while deployed selectors migrate, but must never be emitted.
"""
from __future__ import annotations

from typing import Any

from authority_contracts import (
    VerificationError,
    _digest,
    _expect_object,
    _expect_string,
    _fail,
    _hash,
    _keys,
)

RELEASE_SELECTOR_SCHEMA = "wikilean.release-selector/v2"
LEGACY_RELEASE_SELECTOR_SCHEMA = "wikilean.release-selector/v1"


def _validate_selection(
    value: Any,
    location: str,
    *,
    exact_manifest: bool,
) -> dict[str, Any]:
    selection = _expect_object(value, location)
    required = {"release_id", "release", "manifest"}
    if exact_manifest:
        required.add("manifest_sha256")
    _keys(selection, location, required)
    release_id = _hash(selection["release_id"], f"{location}.release_id")
    release_hex = _digest(selection["release"], f"{location}.release")
    if release_id != f"sha256:{release_hex}":
        _fail(location, "release must be the lowercase digest suffix of release_id")
    namespace = (
        _digest(selection["manifest_sha256"], f"{location}.manifest_sha256")
        if exact_manifest
        else release_hex
    )
    expected_manifest = f"/assets/brain/releases/{namespace}/release.json"
    if selection["manifest"] != expected_manifest:
        _fail(f"{location}.manifest", f"expected {expected_manifest!r}")
    return selection


def validate_release_selector(selector: Any) -> dict[str, Any]:
    """Validate a v2 selector or a read-only legacy v1 selector."""
    obj = _expect_object(selector, "$")
    schema = obj.get("schema")
    exact_manifest = schema == RELEASE_SELECTOR_SCHEMA
    if schema not in {RELEASE_SELECTOR_SCHEMA, LEGACY_RELEASE_SELECTOR_SCHEMA}:
        _fail("$.schema", f"unknown schema/version {schema!r}")

    current_keys = {"release_id", "release", "manifest"}
    previous_keys = {"previous_release_id", "previous_release", "previous_manifest"}
    if exact_manifest:
        current_keys.add("manifest_sha256")
        previous_keys.add("previous_manifest_sha256")
    _keys(obj, "$", {"schema"} | current_keys, previous_keys | {"audited_at"})
    _validate_selection(
        {key: obj[key] for key in current_keys},
        "$",
        exact_manifest=exact_manifest,
    )

    present_previous = previous_keys.intersection(obj)
    if present_previous and present_previous != previous_keys:
        _fail("$", "previous release fields must be present together")
    if present_previous:
        previous_selection = {
            "release_id": obj["previous_release_id"],
            "release": obj["previous_release"],
            "manifest": obj["previous_manifest"],
        }
        if exact_manifest:
            previous_selection["manifest_sha256"] = obj["previous_manifest_sha256"]
        previous = _validate_selection(
            previous_selection,
            "$.previous",
            exact_manifest=exact_manifest,
        )
        if exact_manifest and previous["manifest_sha256"] == obj["manifest_sha256"]:
            _fail(
                "$.previous_manifest_sha256",
                "previous manifest must differ from current manifest",
            )
        if not exact_manifest and previous["release_id"] == obj["release_id"]:
            _fail("$.previous_release_id", "previous release must differ from current release")
    if "audited_at" in obj:
        _expect_string(obj["audited_at"], "$.audited_at")
    return obj


__all__ = [
    "LEGACY_RELEASE_SELECTOR_SCHEMA",
    "RELEASE_SELECTOR_SCHEMA",
    "VerificationError",
    "validate_release_selector",
]
