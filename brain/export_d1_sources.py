#!/usr/bin/env python3
"""Export a verified D1 bundle as private, immutable v3 source-plan fragments.

The export never reads existing annotation sidecars or acquires production data.
It preserves the original receipt and lineage, then records a separate derived
lineage for fresh annotation sidecars and an explicitly verified empty community
edge file. Nonempty community snapshots require a sealed node universe and are
deliberately unsupported here. Source fragments require review and integration
with a complete v3 plan; this command does not claim a publishable offline pack.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import secrets
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "brain" / "tools"
sys.path.insert(0, str(TOOLS))
import authority_contracts as contracts  # noqa: E402
import compile_offline_pack_v2 as publisher  # noqa: E402
import source_plan_contracts  # noqa: E402
import d1_snapshot_bundle as snapshots  # noqa: E402

EXPORT_SCHEMA = "wikilean.d1-source-export/v1"
EXPORT_DOMAIN = "wikilean.d1-source-export.v1"
FRAGMENT_SCHEMA = "wikilean.d1-source-plan-fragment/v1"
NORMALIZATION_SCHEMA = "wikilean.d1-brain-sidecars/v1"
PHYSICAL_ROOT = "d1_export"
DERIVED_SOURCE = "wikilean-d1-brain-sidecars"
IMPLEMENTATION_PATHS = (
    "brain/build_context.py",
    "brain/d1_snapshot_bundle.py",
    "brain/export_d1_sources.py",
    "brain/tools/authority_contracts.py",
    "brain/tools/compile_offline_pack_v2.py",
    "brain/tools/execution_environment.py",
    "brain/tools/source_plan_contracts.py",
)
# Retain the source generation used by this process. A later on-disk edit may
# not be recorded as the implementation that performed this normalization.
LOADED_IMPLEMENTATION = {path: (ROOT / path).read_bytes() for path in IMPLEMENTATION_PATHS}


class ExportError(RuntimeError):
    """A verified, private source export cannot be completed."""


def _verify_implementation() -> dict[str, bytes]:
    current = {path: (ROOT / path).read_bytes() for path in IMPLEMENTATION_PATHS}
    if current != LOADED_IMPLEMENTATION:
        raise ExportError("export implementation changed after this process loaded")
    return dict(LOADED_IMPLEMENTATION)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ref(path: str, data: bytes, media_type: str) -> dict[str, Any]:
    return {"path": path, "sha256": digest(data), "bytes": len(data), "media_type": media_type}


def _object_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"object": item["name"], **{key: item[key] for key in ("sha256", "bytes", "media_type")}}


def build_export(
    bundle_path: Path,
    captured: Mapping[str, bytes],
    *,
    normalized_at: str,
    implementation: Mapping[str, bytes],
    registry_bytes: bytes,
) -> dict[str, bytes]:
    """Derive a complete export from one captured immutable byte generation."""
    captured = dict(captured)
    bundle = snapshots.verify_snapshot_bundle_files(bundle_path, captured)
    if bundle.edges or bundle.nodes:
        raise ExportError("nonempty community snapshots require an explicit sealed node universe; unsupported")
    registry = contracts.parse_json_bytes(registry_bytes, location="source registry")
    annotation_license = registry.get("our_data_license", {}).get("annotations")
    if annotation_license != "CC0-1.0":
        raise ExportError("source registry does not declare the reviewed CC0-1.0 annotation policy")
    if set(implementation) != set(IMPLEMENTATION_PATHS) \
            or any(type(value) is not bytes for value in implementation.values()):
        raise ExportError("export implementation closure does not match the required files")
    files = {"acquisition/" + path: data for path, data in captured.items()}
    receipt = contracts.parse_json_bytes(captured["acquisition-receipt.json"], location="acquisition receipt")
    original_lineage = contracts.parse_json_bytes(captured["normalization-lineage.json"], location="original lineage")
    bundle_manifest = contracts.parse_json_bytes(captured["bundle.json"], location="acquisition bundle")

    configuration = {
        "schema": NORMALIZATION_SCHEMA,
        "annotations": "every sealed article field; no ambient sidecar metadata",
        "filenames": "NFC-normalized slug plus .json; preserve original slug inside payload",
        "community": "only verified zero brain_edges and zero brain_nodes; emit an empty edge file",
        "registry_sha256": digest(registry_bytes),
        "annotation_license": annotation_license,
        "redistribution": "restricted",
        "reason": "private captured database and lineage; no public export policy approved",
    }
    configuration_bytes = contracts.canonical_json_bytes(configuration)
    files["implementation/configuration.json"] = configuration_bytes
    files["implementation/source-registry.json"] = registry_bytes
    toolchain = []
    for logical_path in sorted(implementation):
        data = implementation[logical_path]
        output_path = "implementation/" + logical_path
        files[output_path] = data
        toolchain.append(_ref(logical_path, data, "text/x-python"))
    toolchain_bytes = contracts.canonical_json_bytes({"files": toolchain, "schema": "wikilean.d1-source-export-tool/v1"})
    files["implementation/tool.json"] = toolchain_bytes
    tool = {"name": "wikilean-d1-source-exporter", "version": "1", "sha256": digest(toolchain_bytes)}

    def planned_object(name: str, path: str, roles: list[str], media_type: str) -> dict[str, Any]:
        data = files[path]
        ref = {"name": name, "roles": sorted(roles), "root": PHYSICAL_ROOT,
               **_ref(path, data, media_type), "redistribution": "restricted"}
        # The standalone prospective manifests can be verified here as well as
        # after the compiler copies these objects into its content store.
        files.setdefault("objects/sha256/" + ref["sha256"], data)
        return ref

    def evidence_ref(path: str, **identity: str) -> dict[str, Any]:
        return {"root": PHYSICAL_ROOT, **_ref(path, files[path], "application/json"), **identity}

    parent_objects = [planned_object("d1_raw", "acquisition/acquired.jsonl", ["raw"], "application/x-ndjson")]
    for name, path in snapshots.NORMALIZED_PATHS.items():
        parent_objects.append(planned_object(name, "acquisition/" + path, ["normalized"],
                                             "application/json" if name == "control" else "application/x-ndjson"))
    for name, path, media in (
        ("request_sql", "acquisition/request.sql", "application/sql"),
        ("toolchain", "acquisition/toolchain.json", "application/json"),
    ):
        parent_objects.append(planned_object(name, path, ["receipt"], media))
    parent = {
        "source": "wikilean-d1", "source_kind": "acquired_dataset", "pin": receipt["pin"],
        "objects": sorted(parent_objects, key=lambda item: item["name"]),
        "license": {"expression": "LicenseRef-Private-D1-Snapshot", "redistribution": "restricted",
                    "notice": "Captured database rows are private source material; no redistribution approval is asserted."},
        "acquisition": receipt["tool"],
        "normalization": {"schema": original_lineage["normalization_schema"], "tool": original_lineage["tool"],
                          "inputs": ["d1_raw"], "outputs": sorted(snapshots.NORMALIZED_PATHS)},
        "evidence": {
            "acquisition_receipts": [evidence_ref("acquisition/acquisition-receipt.json",
                                                   acquisition_receipt_id=bundle.acquisition_receipt_id)],
            "normalization_lineage": evidence_ref("acquisition/normalization-lineage.json",
                                                   normalization_lineage_id=bundle.normalization_lineage_id),
            "request_parameter_preimages": [evidence_ref("acquisition/request.json",
                                                         parameters_sha256=digest(captured["request.json"]))],
        },
        "audit": {"acquired_at": bundle.acquired_at, "upstream_uri": receipt["upstream_uri"]},
    }
    parent_manifest = source_plan_contracts._source_manifest_from_plan(parent, "$.parent")

    raw_objects = [{**item, "roles": ["raw"]} for item in parent_objects if "normalized" in item["roles"]]
    normalized_objects = []
    annotation_members = []
    for row in bundle.articles:
        logical_path = "site/annotations/" + unicodedata.normalize("NFC", row["slug"]) + ".json"
        contracts.validate_literal_relative_path(logical_path, "annotation location")
        path = "normalized/" + logical_path
        # A JSON document has no JSONL terminator. This also keeps a one-row
        # article collection distinct from its differently typed JSON sidecar.
        files[path] = contracts.canonical_artifact_json_bytes(dict(row))
        name = "article-" + digest(row["slug"].encode("utf-8"))
        normalized_objects.append(planned_object(name, path, ["normalized"], "application/json"))
        annotation_members.append({"path": logical_path, "source": DERIVED_SOURCE, "object": name})
    community_path = "normalized/brain/data/community_edges.jsonl"
    files[community_path] = b""
    normalized_objects.append(planned_object("community_edges", community_path, ["normalized"], "application/x-ndjson"))
    inputs = [{**_object_ref(item), "origin": {"kind": "source_manifest", "id": parent_manifest["source_manifest_id"]}}
              for item in sorted(raw_objects, key=lambda item: item["name"])]
    lineage = {
        "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
        "source": DERIVED_SOURCE, "mode": "transform", "acquisition_receipt_ids": [],
        "parent_source_manifest_ids": [parent_manifest["source_manifest_id"]],
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": digest(configuration_bytes),
        "tool": tool, "inputs": inputs,
        "outputs": [_object_ref(item) for item in sorted(normalized_objects, key=lambda item: item["name"])],
        "result": "complete", "audit": {"normalized_at": normalized_at},
    }
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    contracts.validate_normalization_lineage(lineage)
    files["evidence/export-lineage.json"] = contracts.canonical_json_bytes(lineage)
    support_objects = [planned_object("export_configuration", "implementation/configuration.json", ["receipt"], "application/json"),
                       planned_object("export_tool", "implementation/tool.json", ["receipt"], "application/json"),
                       planned_object("license_registry", "implementation/source-registry.json", ["receipt"], "application/json")]
    for index, logical_path in enumerate(sorted(implementation)):
        support_objects.append(planned_object(f"export_program_{index}", "implementation/" + logical_path,
                                              ["receipt"], "text/x-python"))
    child = {
        "source": DERIVED_SOURCE, "source_kind": "sealed_snapshot",
        "pin": {"type": "database_snapshot", "value": parent_manifest["source_manifest_id"]},
        "objects": sorted([*raw_objects, *normalized_objects, *support_objects], key=lambda item: item["name"]),
        "license": {"expression": annotation_license, "redistribution": "restricted",
                    "notice": "Registry annotation license recorded; this source pack and its original D1 evidence remain private."},
        "acquisition": tool,
        "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": tool,
                          "inputs": sorted(item["name"] for item in raw_objects),
                          "outputs": sorted(item["name"] for item in normalized_objects)},
        "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [],
                     "normalization_lineage": evidence_ref("evidence/export-lineage.json",
                                                            normalization_lineage_id=lineage["normalization_lineage_id"])},
    }
    child_manifest = source_plan_contracts._source_manifest_from_plan(child, "$.child")
    contracts.validate_source_manifest_evidence_documents(
        parent_manifest, receipts={bundle.acquisition_receipt_id: receipt}, lineage=original_lineage,
        request_parameter_preimages={digest(captured["request.json"]): {
            "parameters_sha256": digest(captured["request.json"]), "bytes": len(captured["request.json"]),
            "media_type": "application/json"}},
    )
    contracts.validate_source_manifest_evidence_documents(
        child_manifest, receipts={}, lineage=lineage, request_parameter_preimages={},
        parent_source_manifests={parent_manifest["source_manifest_id"]: parent_manifest},
    )
    for name, manifest in (("parent", parent_manifest), ("derived", child_manifest)):
        files[f"source-manifests/{name}.json"] = contracts.canonical_json_bytes(manifest)
    fragment = {
        "schema": FRAGMENT_SCHEMA, "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted",
        "sources": sorted([parent, child], key=lambda item: item["source"]),
        "input_bindings": [
            {"input_id": "annotations", "sources": [DERIVED_SOURCE], "state": "present",
             "members": sorted(annotation_members, key=lambda item: item["path"])},
            {"input_id": "brain-community-edges", "sources": [DERIVED_SOURCE], "state": "present",
             "members": [{"path": "brain/data/community_edges.jsonl", "source": DERIVED_SOURCE, "object": "community_edges"}]},
        ],
    }
    files["source-fragment.json"] = contracts.canonical_json_bytes(fragment)
    manifest = {
        "schema": EXPORT_SCHEMA, "bundle_id": bundle_manifest["bundle_id"],
        "parent_source_manifest_id": parent_manifest["source_manifest_id"],
        "derived_source_manifest_id": child_manifest["source_manifest_id"],
        "articles": len(bundle.articles), "brain_edges": 0, "brain_nodes": 0,
        "files": [_ref(path, files[path], "application/octet-stream") for path in sorted(files)],
    }
    manifest["export_id"] = contracts.domain_hash(EXPORT_DOMAIN, manifest)
    files["export.json"] = contracts.canonical_json_bytes(manifest)
    return files


def _verify_written(root: Path, files: Mapping[str, bytes]) -> None:
    publisher._verify_read_only_tree(root)
    if publisher._tree_paths(root) != set(files):
        raise ExportError("published export has an unexpected file closure")
    for path, expected in files.items():
        if contracts.digest_file(root / path) != (digest(expected), len(expected)):
            raise ExportError(f"published export bytes differ: {path}")
    for name in ("parent", "derived"):
        manifest, _ = contracts.load_canonical_json(root / f"source-manifests/{name}.json")
        contracts.verify_source_manifest_files(contracts.validate_source_manifest(manifest), root)


def export_bundle(bundle_path: Path, store_path: Path, *, normalized_at: str | None = None) -> Path:
    """Publish a complete private export; never modify the captured bundle or corpus."""
    bundle_path = Path(bundle_path).absolute()
    store_path, exists = publisher._resolve_output_store(store_path)
    if bundle_path == store_path or bundle_path in store_path.parents or store_path in bundle_path.parents:
        raise ExportError("export store and acquisition bundle must be disjoint by ancestry")
    files = build_export(
        bundle_path, snapshots._bundle_bytes(bundle_path),
        normalized_at=normalized_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        implementation=_verify_implementation(),
        registry_bytes=(ROOT / "catalog/data/source_registry.json").read_bytes(),
    )
    document = contracts.parse_json_bytes(files["export.json"], location="export manifest")
    target_name = document["export_id"].removeprefix("sha256:")
    store, store_identity = (store_path, publisher._validate_private_store(store_path)) if exists \
        else publisher._create_output_store(store_path)
    store_descriptor = publisher._open_private_store(store, store_identity)
    staging_name = ".d1-export-" + secrets.token_hex(12)
    staging_identity = None
    target = store / target_name

    def verify_store() -> None:
        publisher._verify_store_descriptor(store_descriptor, store_identity)
        publisher._verify_directory_identity(store, store_identity, "D1 export store")

    try:
        staging_identity = publisher._create_private_staging(store_descriptor, store_identity, staging_name)
        staging = store / staging_name
        for relative, data in files.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        publisher._make_read_only(staging)
        publisher._fsync_tree(staging)
        _verify_written(staging, files)
        publisher._verify_directory_identity_at(store_descriptor, staging_name, staging_identity, "D1 export staging")
        verify_store()
        _verify_implementation()
        try:
            publisher._publish_no_replace(store_descriptor, staging_name, target_name)
        except publisher._DestinationExists:
            target_identity = publisher._directory_identity_at(store_descriptor, target_name, "existing D1 export")
            _verify_written(target, files)
            publisher._verify_directory_identity_at(store_descriptor, target_name, target_identity, "existing D1 export")
            verify_store()
            publisher._remove_tree_at(store_descriptor, staging_name, staging_identity)
            verify_store()
            return target
        publisher._verify_directory_identity_at(store_descriptor, target_name, staging_identity, "D1 export")
        _verify_written(target, files)
        publisher._verify_directory_identity_at(store_descriptor, target_name, staging_identity, "D1 export")
        verify_store()
        os.fsync(store_descriptor)
        return target
    except BaseException as original_error:
        if staging_identity is not None:
            for name in (staging_name, target_name):
                # A different entry at one name must neither be deleted nor
                # prevent cleanup of our inode at the other publication name.
                try:
                    metadata = os.stat(name, dir_fd=store_descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode) and \
                            publisher._identity_from_stat(metadata, "D1 export cleanup") == staging_identity:
                        publisher._remove_tree_at(store_descriptor, name, staging_identity)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    original_error.add_note(f"D1 export cleanup failed at {name}: {cleanup_error}")
        raise
    finally:
        os.close(store_descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-bundle", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = export_bundle(args.snapshot_bundle, args.store)
    except (ExportError, snapshots.SnapshotBundleError, publisher.PackCompilationError,
            contracts.VerificationError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
