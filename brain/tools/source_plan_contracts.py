#!/usr/bin/env python3
"""Normative validation for evidence-bearing offline-pack source-plan/v3.

The existing v1 plan remains the input to the v2 compiler.  This module layers
the explicit v3 acquisition boundary on the unchanged v1 structural contract;
it does not make the v2 compiler accept or synthesize v3 authority evidence.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
if str(BRAIN) not in sys.path:
    sys.path.insert(0, str(BRAIN))

import authority_contracts as contracts  # noqa: E402

SOURCE_PLAN_SCHEMA_V1 = "wikilean.offline-pack-source-plan/v1"


class SourcePlanContractError(contracts.VerificationError):
    """A v3 source plan or its evidence closure is invalid."""


def _fail(location: str, message: str) -> None:
    raise SourcePlanContractError(f"{location}: {message}")


def _compatibility_plan(plan: dict[str, Any]) -> dict[str, Any]:
    compatibility = copy.deepcopy(plan)
    compatibility["schema"] = SOURCE_PLAN_SCHEMA_V1
    for source in compatibility["sources"]:
        source.pop("evidence", None)
    return compatibility


def _plan_evidence_file(
    value: Any,
    location: str,
    *,
    identity_field: str | None = None,
    preimage: bool = False,
) -> dict[str, Any]:
    required = {"root", "path", "sha256", "bytes", "media_type"}
    if identity_field is not None:
        required.add(identity_field)
    if preimage:
        required.add("parameters_sha256")
    ref = contracts._expect_object(value, location)
    contracts._keys(ref, location, required)
    contracts._expect_pattern(
        ref["root"], f"{location}.root", contracts.NAME_RE, "a lowercase root name"
    )
    contracts.validate_literal_relative_path(ref["path"], f"{location}.path")
    digest = contracts._digest(ref["sha256"], f"{location}.sha256")
    contracts._expect_int(ref["bytes"], f"{location}.bytes")
    contracts._expect_pattern(
        ref["media_type"],
        f"{location}.media_type",
        contracts.MEDIA_TYPE_RE,
        "a media type",
    )
    if identity_field is not None:
        contracts._hash(ref[identity_field], f"{location}.{identity_field}")
        if ref["media_type"] != "application/json":
            _fail(f"{location}.media_type", "expected 'application/json'")
    if preimage:
        parameters_sha256 = contracts._digest(
            ref["parameters_sha256"], f"{location}.parameters_sha256"
        )
        if digest != parameters_sha256:
            _fail(
                f"{location}.sha256",
                "must equal parameters_sha256 because it identifies the exact preimage bytes",
            )
    return ref


def _manifest_evidence_from_plan(
    evidence: dict[str, Any],
    location: str,
    *,
    source_kind: str,
) -> dict[str, Any]:
    evidence = contracts._expect_object(evidence, location)
    contracts._keys(
        evidence,
        location,
        {
            "acquisition_receipts",
            "normalization_lineage",
            "request_parameter_preimages",
        },
    )
    receipts = contracts._expect_array(
        evidence["acquisition_receipts"],
        f"{location}.acquisition_receipts",
        nonempty=source_kind == "acquired_dataset",
    )
    receipt_ids: list[str] = []
    for index, item in enumerate(receipts):
        item_location = f"{location}.acquisition_receipts[{index}]"
        ref = _plan_evidence_file(
            item,
            item_location,
            identity_field="acquisition_receipt_id",
        )
        receipt_ids.append(ref["acquisition_receipt_id"])
    if receipt_ids != sorted(set(receipt_ids)):
        _fail(
            f"{location}.acquisition_receipts",
            "entries must have unique IDs and be sorted by acquisition_receipt_id",
        )

    lineage_ref = _plan_evidence_file(
        evidence["normalization_lineage"],
        f"{location}.normalization_lineage",
        identity_field="normalization_lineage_id",
    )

    preimages = contracts._expect_array(
        evidence["request_parameter_preimages"],
        f"{location}.request_parameter_preimages",
        nonempty=source_kind == "acquired_dataset",
    )
    preimage_digests: list[str] = []
    manifest_preimages: list[dict[str, Any]] = []
    for index, item in enumerate(preimages):
        item_location = f"{location}.request_parameter_preimages[{index}]"
        ref = _plan_evidence_file(item, item_location, preimage=True)
        preimage_digests.append(ref["parameters_sha256"])
        manifest_preimages.append(
            {
                key: ref[key]
                for key in ("parameters_sha256", "bytes", "media_type")
            }
        )
    if preimage_digests != sorted(set(preimage_digests)):
        _fail(
            f"{location}.request_parameter_preimages",
            "entries must have unique digests and be sorted by parameters_sha256",
        )
    return {
        "acquisition_receipt_ids": receipt_ids,
        "normalization_lineage_id": lineage_ref["normalization_lineage_id"],
        "request_parameter_preimages": manifest_preimages,
    }


def _source_manifest_from_plan(source: dict[str, Any], location: str) -> dict[str, Any]:
    manifest = {
        key: copy.deepcopy(value)
        for key, value in source.items()
        if key != "evidence"
    }
    manifest["schema"] = contracts.SOURCE_SCHEMA_V3
    manifest["source_manifest_id"] = "sha256:" + "0" * 64
    manifest["objects"] = [
        {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"root", "path"}
        }
        | {"path": f"objects/sha256/{item['sha256']}"}
        for item in source["objects"]
    ]
    if source["source_kind"] != "curated_git_tree":
        manifest["evidence"] = _manifest_evidence_from_plan(
            source["evidence"],
            f"{location}.evidence",
            source_kind=source["source_kind"],
        )
    manifest["source_manifest_id"] = contracts.source_manifest_identity(manifest)
    contracts.validate_source_manifest(manifest)
    return manifest


def validate_source_plan_v3(value: Any) -> dict[str, Any]:
    """Validate v3 plan shape and derive every prospective source-manifest/v3."""
    if not isinstance(value, dict):
        _fail("$", "expected an object")
    if value.get("schema") != contracts.OFFLINE_PACK_SOURCE_PLAN_SCHEMA_V3:
        _fail(
            "$.schema",
            f"expected {contracts.OFFLINE_PACK_SOURCE_PLAN_SCHEMA_V3!r}",
        )
    # Import lazily so the v2 compiler can later dispatch to this validator
    # without creating a module-initialization cycle.
    import compile_offline_pack_v2 as compiler

    try:
        compiler.validate_source_plan(_compatibility_plan(value))
    except compiler.PackCompilationError as exc:
        raise SourcePlanContractError(str(exc)) from exc

    physical_evidence: dict[tuple[str, str], tuple[Any, ...]] = {}
    logical_evidence: dict[tuple[str, str], tuple[Any, ...]] = {}
    for index, source in enumerate(value["sources"]):
        location = f"$.sources[{index}]"
        if source["source_kind"] == "curated_git_tree":
            if "evidence" in source:
                _fail(
                    f"{location}.evidence",
                    "curated_git_tree sources must not declare acquisition evidence",
                )
            manifest = _source_manifest_from_plan(source, location)
            contracts.validate_source_manifest(manifest)
            continue
        if "evidence" not in source:
            _fail(
                f"{location}.evidence",
                "non-Git source-plan/v3 entries require acquisition receipts, normalization lineage, and request-parameter preimages",
            )
        _source_manifest_from_plan(source, location)
        evidence = source["evidence"]
        refs = [
            *evidence["acquisition_receipts"],
            evidence["normalization_lineage"],
            *evidence["request_parameter_preimages"],
        ]
        for ref in refs:
            key = (ref["root"], ref["path"])
            identity = tuple(
                (field, ref[field])
                for field in sorted(ref)
            )
            prior = physical_evidence.setdefault(key, identity)
            if prior != identity:
                _fail(
                    f"{location}.evidence",
                    f"physical evidence file {key!r} has conflicting metadata",
                )
            if "acquisition_receipt_id" in ref:
                logical_key = ("acquisition_receipt", ref["acquisition_receipt_id"])
            elif "normalization_lineage_id" in ref:
                logical_key = ("normalization_lineage", ref["normalization_lineage_id"])
            else:
                logical_key = ("request_parameter_preimage", ref["parameters_sha256"])
            logical_identity = tuple(
                (field, ref[field])
                for field in ("sha256", "bytes", "media_type")
            )
            prior_logical = logical_evidence.setdefault(logical_key, logical_identity)
            if prior_logical != logical_identity:
                _fail(
                    f"{location}.evidence",
                    f"logical evidence object {logical_key!r} has conflicting bytes or media type",
                )
    return value


def source_manifests_from_plan_v3(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    validate_source_plan_v3(plan)
    manifests: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(plan["sources"]):
        manifest = _source_manifest_from_plan(source, f"$.sources[{index}]")
        manifests[manifest["source_manifest_id"]] = manifest
    return manifests


def _root_for_ref(
    roots: Mapping[str, Path], ref: dict[str, Any], location: str
) -> Path:
    root_name = ref["root"]
    if root_name not in roots:
        _fail(f"{location}.root", f"no physical root supplied for {root_name!r}")
    root = Path(roots[root_name])
    if root.is_symlink():
        _fail(f"{location}.root", "symlink roots are forbidden")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise SourcePlanContractError(
            f"{location}.root: cannot resolve physical root: {exc}"
        ) from exc
    if not root.is_dir():
        _fail(f"{location}.root", "expected a directory")
    return root


def _file_ref_without_root(ref: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ref[key]
        for key in ("path", "sha256", "bytes", "media_type")
    }


def verify_source_plan_v3_evidence(
    plan: dict[str, Any],
    roots: Mapping[str, Path],
) -> dict[str, int]:
    """Read and cross-validate every v3 evidence document and preimage."""
    validate_source_plan_v3(plan)
    manifests = source_manifests_from_plan_v3(plan)
    receipt_ids_seen: set[str] = set()
    lineage_ids_seen: set[str] = set()
    preimage_digests: set[str] = set()
    parent_edges: dict[str, list[str]] = {}

    for index, source in enumerate(plan["sources"]):
        manifest = _source_manifest_from_plan(source, f"$.sources[{index}]")
        manifest_id = manifest["source_manifest_id"]
        if source["source_kind"] == "curated_git_tree":
            parent_edges[manifest_id] = []
            continue
        evidence = source["evidence"]
        receipts: dict[str, dict[str, Any]] = {}
        for receipt_index, ref in enumerate(evidence["acquisition_receipts"]):
            location = f"$.sources[{index}].evidence.acquisition_receipts[{receipt_index}]"
            root = _root_for_ref(roots, ref, location)
            raw = contracts.verify_file_ref(
                root,
                _file_ref_without_root(ref),
                location,
            )
            document = contracts.parse_json_bytes(raw, location=ref["path"])
            if raw != contracts.canonical_json_bytes(document):
                _fail(location, "acquisition receipt is not canonical-json-v1 bytes")
            contracts.validate_acquisition_receipt(document, location=f"{location}.document")
            receipt_id = ref["acquisition_receipt_id"]
            if document["acquisition_receipt_id"] != receipt_id:
                _fail(f"{location}.acquisition_receipt_id", "does not match receipt document")
            receipts[receipt_id] = document
            receipt_ids_seen.add(receipt_id)

        lineage_ref = evidence["normalization_lineage"]
        lineage_location = f"$.sources[{index}].evidence.normalization_lineage"
        lineage_root = _root_for_ref(roots, lineage_ref, lineage_location)
        lineage_raw = contracts.verify_file_ref(
            lineage_root,
            _file_ref_without_root(lineage_ref),
            lineage_location,
        )
        lineage = contracts.parse_json_bytes(lineage_raw, location=lineage_ref["path"])
        if lineage_raw != contracts.canonical_json_bytes(lineage):
            _fail(lineage_location, "normalization lineage is not canonical-json-v1 bytes")
        contracts.validate_normalization_lineage(
            lineage, location=f"{lineage_location}.document"
        )
        if lineage["normalization_lineage_id"] != lineage_ref["normalization_lineage_id"]:
            _fail(
                f"{lineage_location}.normalization_lineage_id",
                "does not match lineage document",
            )
        lineage_ids_seen.add(lineage["normalization_lineage_id"])

        preimages: dict[str, dict[str, Any]] = {}
        for preimage_index, ref in enumerate(evidence["request_parameter_preimages"]):
            location = f"$.sources[{index}].evidence.request_parameter_preimages[{preimage_index}]"
            preimage_root = _root_for_ref(roots, ref, location)
            contracts.verify_file_ref_integrity(
                preimage_root,
                _file_ref_without_root(ref),
                location,
            )
            digest = ref["parameters_sha256"]
            preimages[digest] = {
                key: ref[key]
                for key in ("parameters_sha256", "sha256", "bytes", "media_type")
            }
            preimage_digests.add(digest)

        parents = lineage["parent_source_manifest_ids"]
        missing_parents = sorted(set(parents) - set(manifests))
        if missing_parents:
            _fail(
                lineage_location,
                "references source manifests absent from the plan: "
                + ", ".join(missing_parents),
            )
        parent_edges[manifest_id] = parents
        contracts.validate_source_manifest_evidence_documents(
            manifest,
            receipts=receipts,
            lineage=lineage,
            request_parameter_preimages=preimages,
            parent_source_manifests={parent_id: manifests[parent_id] for parent_id in parents},
            location=f"$.sources[{index}]",
        )

    contracts._validate_source_manifest_parent_dag(
        parent_edges,
        location="$.sources",
    )
    return {
        "source_manifests": len(manifests),
        "acquisition_receipts": len(receipt_ids_seen),
        "normalization_lineages": len(lineage_ids_seen),
        "request_parameter_preimages": len(preimage_digests),
    }
