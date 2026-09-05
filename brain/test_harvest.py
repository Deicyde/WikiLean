#!/usr/bin/env python3
"""Hermetic tests for sealed community-edge graduation."""
from __future__ import annotations

import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acquire_d1_snapshot as acquisition  # noqa: E402
import d1_snapshot_bundle as bundle_verifier  # noqa: E402
import harvest_community_edges as harvest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import authority_contracts as contracts  # noqa: E402

AUDIT_TIME = "2026-09-05T00:00:00Z"
FAKE_TOOLCHAIN = {
    "schema": bundle_verifier.TOOLCHAIN_SCHEMA,
    "invocation": {
        "config_sha256": bundle_verifier.CONFIG_SHA256,
        "database_binding": bundle_verifier.D1_BINDING,
        "forwarded_environment": bundle_verifier.FORWARDED_ENVIRONMENT,
        "forced_environment": bundle_verifier.FORCED_ENVIRONMENT,
    },
    "node": {"version": "v22.0.0", "sha256": "1" * 64},
    "python": {
        "implementation": "CPython",
        "version": "3.12.14+meta",
        "sha256": "2" * 64,
        "startup_flags": bundle_verifier.REQUIRED_PYTHON_STARTUP_FLAGS,
    },
    "local_dependencies": list(bundle_verifier.LOCAL_DEPENDENCY_PINS),
    "wrangler": {
        "version": bundle_verifier.WRANGLER_VERSION,
        "package_integrity": bundle_verifier.WRANGLER_INTEGRITY,
        "cli_sha256": bundle_verifier.WRANGLER_CLI_SHA256,
        "package_lock_sha256": bundle_verifier.PACKAGE_LOCK_SHA256,
    },
    "wrapper": {"sha256": bundle_verifier.ACQUIRER_WRAPPER_SHA256},
}
FAKE_TOOL = {
    "name": "wikilean-d1-acquirer",
    "version": "1",
    "sha256": acquisition._sha256(contracts.canonical_json_bytes(FAKE_TOOLCHAIN)),
}


def _payload(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def article(slug: str = "Abelian_group") -> dict:
    return {
        "record_type": "article",
        "record_key": slug,
        "payload": _payload({
            "slug": slug,
            "wikipedia_title": "Abelian group",
            "display_title": "Abelian group",
            "wikidata_qid": "Q181296",
            "revid": 123,
            "latest_revid": 124,
            "last_upstream_check": 1_700_000_000_000,
            "annotations": "[]",
            "schema_version": 3,
            "version": 1,
            "n_formalized": 0,
            "n_partial": 0,
            "n_not_formalized": 0,
            "created_at": 1_600_000_000_000,
            "updated_at": 1_700_000_000_000,
        }),
    }


def edge(
    edge_id: str,
    *,
    src: str = "Q181296",
    dst: str = "decl:Mathlib:CommGroup",
    kind: str = "formalizes",
    actor: str = "human",
    status: str = "live",
) -> dict:
    deleted = status == "deleted"
    return {
        "record_type": "brain_edge",
        "record_key": edge_id,
        "payload": _payload({
            "id": edge_id,
            "src": src,
            "dst": dst,
            "kind": kind,
            "evidence": '{"note":"reviewed"}',
            "added_by": "jack",
            "actor_type": actor,
            "status": status,
            "created_at": 1_700_000_000_001,
            "deleted_by": "jack" if deleted else None,
            "deleted_at": 1_700_000_000_002 if deleted else None,
            "version": 2 if deleted else 1,
        }),
    }


def node(qid: str = "Q9999", *, status: str = "live") -> dict:
    deleted = status == "deleted"
    return {
        "record_type": "brain_node",
        "record_key": qid,
        "payload": _payload({
            "id": qid,
            "label": "Community concept",
            "description": None,
            "node_type": "concept",
            "added_by": "jack",
            "actor_type": "human",
            "status": status,
            "created_at": 1_700_000_000_003,
            "deleted_by": "jack" if deleted else None,
            "deleted_at": 1_700_000_000_004 if deleted else None,
            "version": 2 if deleted else 1,
        }),
    }


def control(*, articles: int, edges: int, nodes: int) -> dict:
    return {
        "record_type": "control",
        "record_key": "counts",
        "payload": _payload({
            "schema": acquisition.CONTROL_SCHEMA,
            "articles": articles,
            "brain_edges": edges,
            "brain_nodes": nodes,
            "article_columns": list(acquisition.ARTICLE_TABLE_COLUMNS),
            "brain_edge_columns": list(acquisition.EDGE_FIELDS),
            "brain_node_columns": list(acquisition.NODE_FIELDS),
            "rows_total": articles + edges + nodes,
        }),
    }


def publish_bundle(root: Path, edges: list[dict], nodes: list[dict]) -> Path:
    rows = [article(), *edges, *nodes, control(articles=1, edges=len(edges), nodes=len(nodes))]
    parsed = acquisition.parse_wrangler_output(json.dumps([{"results": rows, "success": True}]))
    bundle_id, files = acquisition._bundle_files(
        parsed,
        acquisition_tool=FAKE_TOOL,
        acquisition_toolchain=FAKE_TOOLCHAIN,
        audit_time=AUDIT_TIME,
    )
    target = root / bundle_id.removeprefix("sha256:")
    target.mkdir(mode=0o700)
    (target / "normalized").mkdir(mode=0o700)
    for relative, data in files.items():
        path = target / relative
        path.write_bytes(data)
        path.chmod(0o644)
    return target


def reseal_edge_mutation(bundle: Path, root: Path, field: str, value: str) -> Path:
    """Build a contract-valid bundle whose D1 row has one invalid trust value."""
    files = {
        relative: (bundle / relative).read_bytes()
        for relative in bundle_verifier.EXPECTED_FILES
    }
    raw_rows = []
    for line in files["acquired.jsonl"].splitlines():
        row = contracts.parse_artifact_json_bytes(line, location="test raw")
        if row["record_type"] == "brain_edge":
            payload = contracts.parse_artifact_json_bytes(
                row["payload"].encode(), location="test edge payload"
            )
            payload[field] = value
            row["payload"] = _payload(payload)
        raw_rows.append(row)
    files["acquired.jsonl"] = b"".join(
        contracts.canonical_artifact_json_bytes(row) + b"\n" for row in raw_rows
    )

    normalized_edges = []
    for line in files["normalized/brain_edges.jsonl"].splitlines():
        row = contracts.parse_artifact_json_bytes(line, location="test normalized edge")
        row[field] = value
        normalized_edges.append(row)
    files["normalized/brain_edges.jsonl"] = b"".join(
        contracts.canonical_artifact_json_bytes(row) + b"\n"
        for row in normalized_edges
    )

    receipt = json.loads(files["acquisition-receipt.json"])
    raw_ref = bundle_verifier._object_ref(
        "d1_raw", files["acquired.jsonl"], "application/x-ndjson"
    )
    receipt["outputs"] = [raw_ref]
    receipt["pin"] = {"type": "content_sha256", "value": raw_ref["sha256"]}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    files["acquisition-receipt.json"] = contracts.canonical_json_bytes(receipt)

    lineage = json.loads(files["normalization-lineage.json"])
    receipt_id = receipt["acquisition_receipt_id"]
    lineage["acquisition_receipt_ids"] = [receipt_id]
    lineage["inputs"] = [
        {**raw_ref, "origin": {"kind": "acquisition_receipt", "id": receipt_id}}
    ]
    object_names = {
        "normalized/articles.jsonl": "articles",
        "normalized/brain_edges.jsonl": "brain_edges",
        "normalized/brain_nodes.jsonl": "brain_nodes",
        "normalized/control.json": "control",
    }
    lineage["outputs"] = [
        bundle_verifier._object_ref(
            object_names[path],
            files[path],
            "application/json" if path.endswith(".json") else "application/x-ndjson",
        )
        for path in (
            "normalized/articles.jsonl",
            "normalized/brain_edges.jsonl",
            "normalized/brain_nodes.jsonl",
            "normalized/control.json",
        )
    ]
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["normalization-lineage.json"] = contracts.canonical_json_bytes(lineage)

    manifest = json.loads(files["bundle.json"])
    lineage_id = lineage["normalization_lineage_id"]
    manifest["bundle_id"] = lineage_id
    manifest["normalization_lineage_id"] = lineage_id
    manifest["acquisition_receipt_id"] = receipt_id
    manifest["members"] = [
        {
            "path": path,
            "sha256": bundle_verifier._sha256(files[path]),
            "bytes": len(files[path]),
            "media_type": bundle_verifier.MEMBER_MEDIA_TYPES[path],
        }
        for path in bundle_verifier.MANIFEST_MEMBER_ORDER
    ]
    files["bundle.json"] = contracts.canonical_json_bytes(manifest)

    target = root / lineage_id.removeprefix("sha256:")
    target.mkdir(mode=0o700)
    (target / "normalized").mkdir(mode=0o700)
    for relative, data in files.items():
        path = target / relative
        path.write_bytes(data)
        path.chmod(0o644)
    return target


def reseal_article_set(bundle: Path, root: Path, articles: list[dict]) -> Path:
    """Reseal arbitrary article wrappers without using producer validation."""
    files = {
        relative: (bundle / relative).read_bytes()
        for relative in bundle_verifier.EXPECTED_FILES
    }
    raw_rows = [
        contracts.parse_artifact_json_bytes(line, location="test raw")
        for line in files["acquired.jsonl"].splitlines()
    ]
    non_articles = [
        row for row in raw_rows if row["record_type"] not in {"article", "control"}
    ]
    control_row = next(row for row in raw_rows if row["record_type"] == "control")
    control_payload = contracts.parse_artifact_json_bytes(
        control_row["payload"].encode(), location="test control"
    )
    control_payload["articles"] = len(articles)
    control_payload["rows_total"] = (
        len(articles)
        + control_payload["brain_edges"]
        + control_payload["brain_nodes"]
    )
    control_row["payload"] = _payload(control_payload)
    raw_rows = [*articles, *non_articles, control_row]
    raw_rows.sort(
        key=lambda row: (
            bundle_verifier.RECORD_ORDER[row["record_type"]],
            row["record_key"].encode(),
        )
    )
    files["acquired.jsonl"] = b"".join(
        contracts.canonical_artifact_json_bytes(row) + b"\n" for row in raw_rows
    )
    normalized_articles = []
    for row in articles:
        payload = contracts.parse_artifact_json_bytes(
            row["payload"].encode(), location="test article"
        )
        payload["annotations"] = contracts.parse_artifact_json_bytes(
            payload["annotations"].encode(), location="test annotations"
        )
        normalized_articles.append(payload)
    normalized_articles.sort(key=lambda row: row["slug"].encode())
    files["normalized/articles.jsonl"] = b"".join(
        contracts.canonical_artifact_json_bytes(row) + b"\n"
        for row in normalized_articles
    )
    files["normalized/control.json"] = contracts.canonical_artifact_json_bytes(
        control_payload
    )

    receipt = json.loads(files["acquisition-receipt.json"])
    raw_ref = bundle_verifier._object_ref(
        "d1_raw", files["acquired.jsonl"], "application/x-ndjson"
    )
    receipt["outputs"] = [raw_ref]
    receipt["pin"] = {"type": "content_sha256", "value": raw_ref["sha256"]}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    files["acquisition-receipt.json"] = contracts.canonical_json_bytes(receipt)

    lineage = json.loads(files["normalization-lineage.json"])
    receipt_id = receipt["acquisition_receipt_id"]
    lineage["acquisition_receipt_ids"] = [receipt_id]
    lineage["inputs"] = [
        {**raw_ref, "origin": {"kind": "acquisition_receipt", "id": receipt_id}}
    ]
    object_names = {
        "normalized/articles.jsonl": "articles",
        "normalized/brain_edges.jsonl": "brain_edges",
        "normalized/brain_nodes.jsonl": "brain_nodes",
        "normalized/control.json": "control",
    }
    lineage["outputs"] = [
        bundle_verifier._object_ref(
            object_names[path],
            files[path],
            "application/json" if path.endswith(".json") else "application/x-ndjson",
        )
        for path in (
            "normalized/articles.jsonl",
            "normalized/brain_edges.jsonl",
            "normalized/brain_nodes.jsonl",
            "normalized/control.json",
        )
    ]
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(
        lineage
    )
    files["normalization-lineage.json"] = contracts.canonical_json_bytes(lineage)

    manifest = json.loads(files["bundle.json"])
    lineage_id = lineage["normalization_lineage_id"]
    manifest["bundle_id"] = lineage_id
    manifest["normalization_lineage_id"] = lineage_id
    manifest["acquisition_receipt_id"] = receipt_id
    manifest["members"] = [
        {
            "path": path,
            "sha256": bundle_verifier._sha256(files[path]),
            "bytes": len(files[path]),
            "media_type": bundle_verifier.MEMBER_MEDIA_TYPES[path],
        }
        for path in bundle_verifier.MANIFEST_MEMBER_ORDER
    ]
    files["bundle.json"] = contracts.canonical_json_bytes(manifest)

    target = root / lineage_id.removeprefix("sha256:")
    target.mkdir(mode=0o700)
    (target / "normalized").mkdir(mode=0o700)
    for relative, data in files.items():
        path = target / relative
        path.write_bytes(data)
        path.chmod(0o644)
    return target


def static_nodes(path: Path) -> None:
    path.write_text(
        json.dumps({"_meta": {"schema": "test"}}) + "\n"
        + json.dumps({"id": "decl:Mathlib:CommGroup"}) + "\n"
        + json.dumps({"id": "Q181296"}) + "\n",
        encoding="utf-8",
    )


class HarvestTests(unittest.TestCase):
    def test_verified_bundle_keeps_tombstone_but_graduates_only_live_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_path = publish_bundle(
                root,
                [
                    edge("bbbbbbbbbbbb", src="Q9999", dst="Q181296"),
                    edge("aaaaaaaaaaaa", status="deleted"),
                ],
                [node()],
            )
            nodes_path = root / "nodes.jsonl"
            output = root / "community.jsonl"
            static_nodes(nodes_path)
            kept, dropped, bundle = harvest.run(
                bundle_path, output=output, static_nodes=nodes_path
            )
            self.assertEqual(bundle.acquired_at, AUDIT_TIME)
            self.assertEqual([row["slug"] for row in bundle.articles], ["Abelian_group"])
            self.assertEqual(len(bundle.edges), 2)
            self.assertEqual([row["status"] for row in bundle.edges], ["deleted", "live"])
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["src"], "Q9999", "live D1-only node must be an endpoint")
            self.assertEqual(dropped, {"deleted": 1})
            self.assertEqual(
                kept[0]["provenance"]["pin"], bundle.normalization_lineage_id
            )
            self.assertTrue(kept[0]["provenance"]["pin"].startswith("sha256:"))
            self.assertEqual(output.read_bytes(), harvest._output_bytes(kept))
            self.assertFalse(list(root.glob(".community.jsonl.*.tmp")))

    def test_tamper_is_rejected_before_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_path = publish_bundle(root, [edge("aaaaaaaaaaaa")], [])
            member = bundle_path / "normalized" / "brain_edges.jsonl"
            member.write_bytes(member.read_bytes() + b"{}\n")
            nodes_path = root / "nodes.jsonl"
            output = root / "community.jsonl"
            static_nodes(nodes_path)
            with self.assertRaisesRegex(harvest.HarvestError, "member digest"):
                harvest.run(bundle_path, output=output, static_nodes=nodes_path)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".community.jsonl.*.tmp")))

    def test_verifier_rejects_resealed_mirror_unsafe_article_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            valid = publish_bundle(source_root, [], [])
            cases = (
                ("unsafe", [article("../escape")], "mirror-safe"),
                ("reserved-case", [article("Draft.AGENT1")], "mirror-safe"),
                ("control", [article("bad\tcontrol")], "mirror-safe"),
                ("too-long", [article("a" * 251)], "exceeds 255"),
                (
                    "collision",
                    [article("Abelian_group"), article("abelian_group")],
                    "collide as mirror filenames",
                ),
                (
                    "unicode-collision",
                    [article("Café"), article("Cafe\u0301")],
                    "collide as mirror filenames",
                ),
                ("empty", [], "at least one article"),
            )
            for name, articles, message in cases:
                altered_root = root / name
                altered_root.mkdir()
                altered = reseal_article_set(valid, altered_root, articles)
                with self.subTest(name=name), self.assertRaisesRegex(
                    bundle_verifier.SnapshotBundleError, message
                ):
                    bundle_verifier.verify_snapshot_bundle(altered)

    def test_mixed_bundle_is_rejected_and_existing_output_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = publish_bundle(first_root, [edge("aaaaaaaaaaaa")], [])
            second = publish_bundle(
                second_root, [edge("bbbbbbbbbbbb", kind="mentions")], []
            )
            shutil.copyfile(
                second / "normalized" / "brain_edges.jsonl",
                first / "normalized" / "brain_edges.jsonl",
            )
            nodes_path = root / "nodes.jsonl"
            output = root / "community.jsonl"
            static_nodes(nodes_path)
            output.write_bytes(b"prior-good-output\n")
            with self.assertRaisesRegex(harvest.HarvestError, "member digest"):
                harvest.run(first, output=output, static_nodes=nodes_path)
            self.assertEqual(output.read_bytes(), b"prior-good-output\n")

    def test_unknown_actor_status_and_kind_are_fatal_not_coerced(self) -> None:
        base = {
            "id": "aaaaaaaaaaaa",
            "src": "Q181296",
            "dst": "decl:Mathlib:CommGroup",
            "kind": "formalizes",
            "evidence": {"note": "reviewed"},
            "added_by": "jack",
            "actor_type": "human",
            "status": "live",
            "created_at": 1,
            "deleted_by": None,
            "deleted_at": None,
            "version": 1,
        }
        universe = {"Q181296", "decl:Mathlib:CommGroup"}
        for field, value, message in (
            ("actor_type", "pipeline", "unknown actor"),
            ("status", "approved", "unknown status"),
            ("kind", "depends", "unknown community kind"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(harvest.HarvestError, message):
                harvest.validate_edge({**base, field: value}, universe, "sha256:" + "a" * 64)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nodes_path = root / "nodes.jsonl"
            static_nodes(nodes_path)
            for index, (field, value, message) in enumerate((
                ("actor_type", "pipeline", "unknown actor"),
                ("status", "approved", "unknown status"),
            )):
                source_root = root / f"source-{index}"
                altered_root = root / f"altered-{index}"
                source_root.mkdir()
                altered_root.mkdir()
                valid = publish_bundle(source_root, [edge("aaaaaaaaaaaa")], [])
                bad_bundle = reseal_edge_mutation(
                    valid, altered_root, field, value
                )
                output = root / f"community-{index}.jsonl"
                with self.assertRaisesRegex(harvest.HarvestError, message):
                    harvest.run(
                        bad_bundle, output=output, static_nodes=nodes_path
                    )
                self.assertFalse(output.exists())

            kind_root = root / "unknown-kind"
            kind_root.mkdir()
            with self.assertRaisesRegex(
                acquisition.D1SnapshotError, "unexpected community kind"
            ):
                publish_bundle(
                    kind_root, [edge("aaaaaaaaaaaa", kind="depends")], []
                )
            self.assertFalse(any(kind_root.iterdir()))

    def test_order_is_canonical_and_independent_of_input_order(self) -> None:
        pin = "sha256:" + "b" * 64
        universe = {"Q181296", "decl:Mathlib:CommGroup"}
        normalized = []
        for item in (
            edge("bbbbbbbbbbbb", kind="mentions"),
            edge("aaaaaaaaaaaa", kind="formalizes"),
        ):
            payload = json.loads(item["payload"])
            payload["evidence"] = json.loads(payload["evidence"])
            normalized.append(payload)
        first, _ = harvest.harvest(normalized, universe, pin)
        second, _ = harvest.harvest(list(reversed(normalized)), universe, pin)
        self.assertEqual(harvest._output_bytes(first), harvest._output_bytes(second))
        lines = harvest._output_bytes(first).splitlines()
        self.assertEqual(lines, sorted(lines))

    def test_both_actor_classes_use_only_the_sealed_node_universe(self) -> None:
        payload = json.loads(edge("aaaaaaaaaaaa", actor="ai")["payload"])
        payload["evidence"] = json.loads(payload["evidence"])
        universe = {"Q181296", "decl:Mathlib:CommGroup"}
        graduated, reason = harvest.validate_edge(
            payload, universe, "sha256:" + "c" * 64
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(graduated)
        self.assertEqual(graduated["confidence"], "medium")
        payload["src"] = "Q9999"
        graduated, reason = harvest.validate_edge(
            payload, universe, "sha256:" + "c" * 64
        )
        self.assertIsNone(graduated)
        self.assertEqual(reason, "src not a known node")

    def test_static_node_metadata_position_and_duplicates_are_strict(self) -> None:
        real_ids = harvest.load_node_ids()
        self.assertIn("Q164", real_ids)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing-meta.jsonl"
            missing.write_text('{"id":"Q1"}\n', encoding="utf-8")
            with self.assertRaisesRegex(harvest.HarvestError, "first line"):
                harvest.load_node_ids(missing)
            misplaced = root / "misplaced-meta.jsonl"
            misplaced.write_text(
                '{"_meta":{"schema":"test"}}\n'
                '{"id":"Q1"}\n'
                '{"_meta":{"schema":"duplicate"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(harvest.HarvestError, "only on the first"):
                harvest.load_node_ids(misplaced)
            duplicate = root / "duplicate-node.jsonl"
            duplicate.write_text(
                '{"_meta":{"schema":"test"}}\n'
                '{"id":"Q1"}\n'
                '{"id":"Q1"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(harvest.HarvestError, "duplicate node id"):
                harvest.load_node_ids(duplicate)

    def test_toolchain_policy_is_exact_except_recorded_node_22_binary(self) -> None:
        bundle_verifier._validate_toolchain(copy.deepcopy(FAKE_TOOLCHAIN))
        cases = [
            (("schema",), "other", "unexpected schema"),
            (("invocation", "database_binding"), "OTHER", "acquisition policy"),
            (("node", "version"), "v20.0.0", "Node 22"),
            (("node", "sha256"), "short", "SHA-256"),
            (("python", "implementation"), "PyPy", "CPython"),
            (("python", "version"), "3.11.9", "Python 3.12"),
            (("python", "sha256"), "short", "SHA-256"),
            (
                ("python", "startup_flags", "no_site"),
                False,
                "isolated -I -S",
            ),
            (
                ("local_dependencies", 0, "sha256"),
                "0" * 64,
                "reviewed local pins",
            ),
            (("wrangler", "version"), "4.121.0", "reviewed pins"),
            (("wrapper", "sha256"), "0" * 64, "unexpected acquirer wrapper"),
        ]
        for keys, value, message in cases:
            with self.subTest(keys=keys):
                candidate = copy.deepcopy(FAKE_TOOLCHAIN)
                target = candidate
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaisesRegex(harvest.HarvestError, message):
                    bundle_verifier._validate_toolchain(candidate)

    def test_root_symlink_swap_is_detected_while_dirfds_keep_reads_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            attack_root = root / "attack"
            source_root.mkdir()
            attack_root.mkdir()
            bundle_path = publish_bundle(
                source_root, [edge("aaaaaaaaaaaa")], []
            )
            attack_bundle = publish_bundle(
                attack_root, [edge("bbbbbbbbbbbb", kind="mentions")], []
            )
            moved = source_root / "original-after-swap"
            real_open = bundle_verifier.os.open
            swapped = False

            def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "normalized" and dir_fd is not None and not swapped:
                    swapped = True
                    bundle_path.rename(moved)
                    bundle_path.symlink_to(attack_bundle, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(bundle_verifier.os, "open", side_effect=swapping_open),
                self.assertRaisesRegex(
                    harvest.HarvestError, "directory changed while being read"
                ),
            ):
                bundle_verifier.verify_snapshot_bundle(bundle_path)
            self.assertTrue(swapped)

    def test_atomic_replace_failure_preserves_prior_output_and_cleans_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "community.jsonl"
            output.write_bytes(b"prior-good-output\n")
            with (
                mock.patch.object(
                    harvest.os, "replace", side_effect=OSError("injected failure")
                ),
                self.assertRaisesRegex(OSError, "injected failure"),
            ):
                harvest.write_output(output, b"replacement\n")
            self.assertEqual(output.read_bytes(), b"prior-good-output\n")
            self.assertFalse(list(root.glob(".community.jsonl.*.tmp")))

    def test_cli_requires_a_snapshot_bundle_and_has_no_live_or_fixture_path(self) -> None:
        self.assertFalse(hasattr(harvest, "read_d1_live"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            harvest.main([])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
