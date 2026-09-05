#!/usr/bin/env python3
"""Hermetic tests for the sealed D1 acquisition boundary."""
from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acquire_d1_snapshot as snapshot  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import authority_contracts as contracts  # noqa: E402


AUDIT_TIME = "2026-09-04T23:59:00Z"
FAKE_TOOLCHAIN = {
    "schema": "wikilean.test-d1-toolchain/v1",
    "node": {"version": "v22.0.0", "sha256": "1" * 64},
    "wrangler": {"version": "test", "cli_sha256": "2" * 64},
}
FAKE_TOOL = {
    "name": "fake-wrangler",
    "version": "1",
    "sha256": snapshot._sha256(contracts.canonical_json_bytes(FAKE_TOOLCHAIN)),
}


def _payload(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def article(slug: str = "Abelian_group") -> dict:
    return {
        "record_type": "article",
        "record_key": slug,
        "payload": _payload(
            {
                "slug": slug,
                "wikipedia_title": "Abelian group",
                "display_title": "Abelian group",
                "wikidata_qid": "Q181296",
                "revid": 123,
                "latest_revid": 124,
                "last_upstream_check": 1_700_000_000_000,
                "annotations": '[{"text":"café","provenance":"human"}]',
                "schema_version": 3,
                "version": 9,
                "n_formalized": 1,
                "n_partial": 0,
                "n_not_formalized": 0,
                "created_at": 1_600_000_000_000,
                "updated_at": 1_700_000_000_000,
            }
        ),
    }


def edge(edge_id: str = "aaaaaaaaaaaa", *, deleted: bool = False) -> dict:
    return {
        "record_type": "brain_edge",
        "record_key": edge_id,
        "payload": _payload(
            {
                "id": edge_id,
                "src": "Q181296",
                "dst": "decl:Mathlib:CommGroup",
                "kind": "formalizes",
                "evidence": '{"note":"reviewed"}',
                "added_by": "jack",
                "actor_type": "human",
                "status": "deleted" if deleted else "live",
                "created_at": 1_700_000_000_001,
                "deleted_by": "jack" if deleted else None,
                "deleted_at": 1_700_000_000_002 if deleted else None,
                "version": 2 if deleted else 1,
            }
        ),
    }


def node(qid: str = "Q5530428") -> dict:
    return {
        "record_type": "brain_node",
        "record_key": qid,
        "payload": _payload(
            {
                "id": qid,
                "label": "GNS construction",
                "description": None,
                "node_type": "concept",
                "added_by": "pipeline",
                "actor_type": "ai",
                "status": "live",
                "created_at": 1_700_000_000_003,
                "deleted_by": None,
                "deleted_at": None,
                "version": 1,
            }
        ),
    }


def control(*, articles: int = 1, edges: int = 2, nodes: int = 1) -> dict:
    return {
        "record_type": "control",
        "record_key": "counts",
        "payload": _payload(
            {
                "schema": snapshot.CONTROL_SCHEMA,
                "articles": articles,
                "brain_edges": edges,
                "brain_nodes": nodes,
                "article_columns": list(snapshot.ARTICLE_TABLE_COLUMNS),
                "brain_edge_columns": list(snapshot.EDGE_FIELDS),
                "brain_node_columns": list(snapshot.NODE_FIELDS),
                "rows_total": articles + edges + nodes,
            }
        ),
    }


def rows() -> list[dict]:
    return [article(), edge(), edge("bbbbbbbbbbbb", deleted=True), node(), control()]


def response(items: list[dict] | None = None) -> str:
    return json.dumps([{"results": rows() if items is None else items, "success": True}])


class FakeRunner:
    def __init__(self, stdout: str, *, mutate_config: bool = False):
        self.stdout = stdout
        self.mutate_config = mutate_config
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        captured = dict(kwargs)
        if "--config" in command:
            config_path = Path(command[command.index("--config") + 1])
            captured["_captured_config"] = config_path.read_text()
            captured["_captured_config_mode"] = stat.S_IMODE(
                config_path.stat().st_mode
            )
            captured["_captured_config_parent_mode"] = stat.S_IMODE(
                config_path.parent.stat().st_mode
            )
            if self.mutate_config:
                config_path.write_text("{}")
        self.calls.append((list(command), captured))
        return subprocess.CompletedProcess(command, 0, self.stdout, "")


class FakeProbeRunner:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        return subprocess.CompletedProcess(command, 0, "v22.99.0\n", "")


class D1SnapshotTests(unittest.TestCase):
    def test_reviewed_query_is_one_read_only_union_cte_and_includes_tombstones(self) -> None:
        sql = snapshot.REQUEST_PATH.read_text()
        self.assertEqual(snapshot._sha256(sql.encode()), snapshot.SQL_SHA256)
        request_descriptor = snapshot.REQUEST_DESCRIPTOR_PATH.read_bytes()
        self.assertEqual(
            snapshot._sha256(request_descriptor), snapshot.REQUEST_PARAMETERS_SHA256
        )
        descriptor = json.loads(request_descriptor)
        self.assertEqual(descriptor["database_id"], snapshot.D1_DATABASE_ID)
        self.assertEqual(descriptor["database_name"], snapshot.D1_DATABASE_NAME)
        self.assertEqual(descriptor["account_id"], snapshot.D1_ACCOUNT_ID)
        self.assertEqual(descriptor["sql_sha256"], snapshot.SQL_SHA256)
        self.assertNotEqual(snapshot.ARTICLE_TABLE_COLUMNS, snapshot.ARTICLE_FIELDS)
        self.assertNotIn(";", sql)
        upper = sql.upper()
        self.assertTrue(upper.lstrip().startswith("WITH "))
        self.assertGreaterEqual(upper.count("UNION ALL"), 3)
        for forbidden in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER "):
            self.assertNotIn(forbidden, " " + upper + " ")
        self.assertNotIn("WHERE STATUS", upper)

        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE articles (
              slug TEXT PRIMARY KEY, wikipedia_title TEXT NOT NULL,
              display_title TEXT NOT NULL, wikidata_qid TEXT, revid INTEGER,
              annotations TEXT NOT NULL, version INTEGER NOT NULL,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              latest_revid INTEGER, last_upstream_check INTEGER,
              schema_version INTEGER NOT NULL, n_formalized INTEGER,
              n_partial INTEGER, n_not_formalized INTEGER
            );
            CREATE TABLE brain_edges (
              id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL,
              kind TEXT NOT NULL, evidence TEXT NOT NULL, added_by TEXT NOT NULL,
              actor_type TEXT NOT NULL, status TEXT NOT NULL,
              created_at INTEGER NOT NULL, deleted_by TEXT, deleted_at INTEGER,
              version INTEGER NOT NULL
            );
            CREATE TABLE brain_nodes (
              id TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT,
              node_type TEXT NOT NULL, added_by TEXT NOT NULL,
              actor_type TEXT NOT NULL, status TEXT NOT NULL,
              created_at INTEGER NOT NULL, deleted_by TEXT, deleted_at INTEGER,
              version INTEGER NOT NULL
            );
            """
        )
        a = json.loads(article()["payload"])
        e1 = json.loads(edge()["payload"])
        e2 = json.loads(edge("bbbbbbbbbbbb", deleted=True)["payload"])
        n = json.loads(node()["payload"])
        connection.execute(
            f"INSERT INTO articles ({','.join(snapshot.ARTICLE_FIELDS)}) "
            f"VALUES ({','.join('?' for _ in snapshot.ARTICLE_FIELDS)})",
            [a[field] for field in snapshot.ARTICLE_FIELDS],
        )
        for value in (e1, e2):
            connection.execute(
                f"INSERT INTO brain_edges ({','.join(snapshot.EDGE_FIELDS)}) "
                f"VALUES ({','.join('?' for _ in snapshot.EDGE_FIELDS)})",
                [value[field] for field in snapshot.EDGE_FIELDS],
            )
        connection.execute(
            f"INSERT INTO brain_nodes ({','.join(snapshot.NODE_FIELDS)}) "
            f"VALUES ({','.join('?' for _ in snapshot.NODE_FIELDS)})",
            [n[field] for field in snapshot.NODE_FIELDS],
        )
        cursor = connection.execute(sql)
        result_rows = [dict(zip((col[0] for col in cursor.description), row)) for row in cursor]
        parsed = snapshot.parse_wrangler_output(
            json.dumps([{"results": result_rows, "success": True}])
        )
        deleted = [
            json.loads(item["payload"])
            for item in parsed
            if item["record_type"] == "brain_edge"
            and item["record_key"] == "bbbbbbbbbbbb"
        ]
        self.assertEqual(deleted[0]["status"], "deleted")

    def test_sealed_config_direct_node_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "node_modules" / "wrangler"
            cli = package / "wrangler-dist" / "cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("console.log('fake');\n")
            node_binary = root / "node"
            node_binary.write_text("#!/bin/sh\nexit 0\n")
            node_binary.chmod(0o755)
            lock = root / "package-lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "packages": {
                            "": {"devDependencies": {"wrangler": snapshot.WRANGLER_VERSION}},
                            "node_modules/wrangler": {
                                "version": snapshot.WRANGLER_VERSION,
                                "integrity": snapshot.WRANGLER_INTEGRITY,
                            },
                        }
                    }
                )
            )
            hostile_config = root / "hostile-wrangler.json"
            hostile_config.write_text(
                json.dumps(
                    {
                        "account_id": "attacker",
                        "d1_databases": [
                            {
                                "binding": snapshot.D1_BINDING,
                                "database_name": snapshot.D1_DATABASE_NAME,
                                "database_id": "attacker-database",
                            },
                            {
                                "binding": "COLLISION",
                                "database_name": snapshot.D1_DATABASE_NAME,
                                "database_id": "second-attacker-database",
                            },
                        ],
                    }
                )
            )
            runner = FakeRunner(response())
            probe = FakeProbeRunner()
            malicious_environment = {
                "NODE_OPTIONS": "--require=/tmp/evil-node-hook.js",
                "NODE_PATH": "/tmp/evil-node-modules",
                "CLOUDFLARE_API_BASE_URL": "https://attacker.invalid",
                "CLOUDFLARE_BASE_URL": "https://attacker.invalid",
                "CF_API_BASE_URL": "https://attacker.invalid",
                "WRANGLER_API_ENVIRONMENT": "staging",
                "WRANGLER_CONFIG_FILENAME": str(hostile_config),
                "CLOUDFLARE_ACCOUNT_ID": "attacker",
                "HTTPS_PROXY": "http://attacker.invalid:8080",
                "CLOUDFLARE_API_TOKEN": "test-secret-token",
            }
            with (
                mock.patch.object(snapshot, "WRANGLER_CLI", cli),
                mock.patch.object(snapshot, "PACKAGE_LOCK", lock),
                mock.patch.object(
                    snapshot, "PACKAGE_LOCK_SHA256", snapshot._file_sha256(lock)
                ),
                mock.patch.object(
                    snapshot, "WRANGLER_CLI_SHA256", snapshot._file_sha256(cli)
                ),
                mock.patch.object(snapshot.shutil, "which", return_value=str(node_binary)),
                mock.patch.dict(os.environ, malicious_environment, clear=False),
            ):
                stdout, tool, toolchain = snapshot.run_wrangler(
                    runner=runner, probe_runner=probe
                )
            self.assertEqual(stdout, response())
            self.assertEqual(tool["name"], "wikilean-d1-acquirer")
            self.assertEqual(tool["sha256"], snapshot._sha256(
                contracts.canonical_json_bytes(toolchain)
            ))
            command, kwargs = runner.calls[0]
            self.assertEqual(command[0], str(node_binary.resolve()))
            self.assertEqual(command[1], "--no-warnings")
            self.assertEqual(command[2], str(cli))
            self.assertNotIn("npx", command)
            self.assertEqual(command.count("--command"), 1)
            self.assertEqual(
                command[command.index("--command") + 1], snapshot.REQUEST_PATH.read_text()
            )
            self.assertIn(snapshot.D1_BINDING, command)
            config_path = Path(command[command.index("--config") + 1])
            self.assertNotEqual(config_path, hostile_config)
            self.assertFalse(config_path.exists(), "private config was not cleaned")
            self.assertEqual(Path(kwargs["cwd"]), config_path.parent)
            config_during_call = json.loads(runner.calls[0][1]["_captured_config"])
            self.assertEqual(kwargs["_captured_config_mode"], 0o600)
            self.assertEqual(kwargs["_captured_config_parent_mode"], 0o700)
            self.assertEqual(config_during_call["account_id"], snapshot.D1_ACCOUNT_ID)
            self.assertEqual(
                config_during_call["d1_databases"],
                [
                    {
                        "binding": snapshot.D1_BINDING,
                        "database_id": snapshot.D1_DATABASE_ID,
                        "database_name": snapshot.D1_DATABASE_NAME,
                    }
                ],
            )
            child_environment = kwargs["env"]
            self.assertEqual(
                child_environment["CLOUDFLARE_API_TOKEN"], "test-secret-token"
            )
            for name in malicious_environment:
                if name != "CLOUDFLARE_API_TOKEN":
                    self.assertNotIn(name, child_environment)
            self.assertEqual(probe.calls[0][0], [str(node_binary.resolve()), "--version"])
            self.assertEqual(probe.calls[0][1]["env"], child_environment)

            mutating_runner = FakeRunner(response(), mutate_config=True)
            with (
                mock.patch.object(
                    snapshot,
                    "_pinned_toolchain",
                    return_value=(node_binary.resolve(), FAKE_TOOL, FAKE_TOOLCHAIN),
                ),
                self.assertRaisesRegex(
                    snapshot.D1SnapshotError, "config changed during execution"
                ),
            ):
                snapshot.run_wrangler(
                    runner=mutating_runner, probe_runner=probe
                )

    def test_malformed_truncated_duplicate_and_wrong_types_fail_closed(self) -> None:
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "invalid JSON"):
            snapshot.parse_wrangler_output('[{"results":')
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "exactly one"):
            snapshot.parse_wrangler_output("[]")
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "duplicate record"):
            snapshot.parse_wrangler_output(response(rows() + [edge()]))
        truncated = [item for item in rows() if item["record_key"] != "Abelian_group"]
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "articles=1"):
            snapshot.parse_wrangler_output(response(truncated))
        wrong = rows()
        bad = json.loads(wrong[0]["payload"])
        bad["version"] = True
        wrong[0] = {**wrong[0], "payload": _payload(bad)}
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "expected integer"):
            snapshot.parse_wrangler_output(response(wrong))
        missing_gravestone = rows()
        bad_edge = json.loads(missing_gravestone[2]["payload"])
        bad_edge["deleted_at"] = None
        missing_gravestone[2] = {
            **missing_gravestone[2],
            "payload": _payload(bad_edge),
        }
        with self.assertRaisesRegex(snapshot.D1SnapshotError, "lacks its gravestone"):
            snapshot.parse_wrangler_output(response(missing_gravestone))

    def test_order_is_canonical_and_raw_and_normalized_bytes_are_clock_free(self) -> None:
        first = snapshot.parse_wrangler_output(response(rows()))
        second = snapshot.parse_wrangler_output(response(list(reversed(rows()))))
        self.assertEqual(first, second)
        first_id, first_files = snapshot._bundle_files(
            first,
            acquisition_tool=FAKE_TOOL,
            acquisition_toolchain=FAKE_TOOLCHAIN,
            audit_time=AUDIT_TIME,
        )
        later_id, later_files = snapshot._bundle_files(
            second,
            acquisition_tool=FAKE_TOOL,
            acquisition_toolchain=FAKE_TOOLCHAIN,
            audit_time="2030-01-01T00:00:00Z",
        )
        self.assertEqual(first_id, later_id)
        for path in (
            "request.sql",
            "request.json",
            "toolchain.json",
            "acquired.jsonl",
            "normalized/articles.jsonl",
            "normalized/brain_edges.jsonl",
            "normalized/brain_nodes.jsonl",
            "normalized/control.json",
            "bundle.json",
        ):
            self.assertEqual(first_files[path], later_files[path], path)
        self.assertNotEqual(
            first_files["acquisition-receipt.json"],
            later_files["acquisition-receipt.json"],
        )

    def test_published_bundle_is_private_complete_and_authority_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "ignored" / "d1" / "snapshots"
            target = snapshot.publish_response(
                response(),
                store=store,
                acquisition_tool=FAKE_TOOL,
                acquisition_toolchain=FAKE_TOOLCHAIN,
                audit_time=AUDIT_TIME,
            )
            self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            receipt = json.loads((target / "acquisition-receipt.json").read_text())
            lineage = json.loads((target / "normalization-lineage.json").read_text())
            contracts.validate_acquisition_receipt(receipt)
            contracts.validate_normalization_lineage(lineage)
            self.assertEqual(target.name, lineage["normalization_lineage_id"].split(":", 1)[1])
            edges = [
                json.loads(line)
                for line in (target / "normalized/brain_edges.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual([item["status"] for item in edges], ["live", "deleted"])
            self.assertEqual(
                receipt["outputs"][0]["sha256"],
                snapshot._sha256((target / "acquired.jsonl").read_bytes()),
            )
            self.assertEqual(
                receipt["tool"]["sha256"],
                snapshot._sha256((target / "toolchain.json").read_bytes()),
            )

    def test_concurrent_publishers_converge_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            barrier = threading.Barrier(2)

            def publish(audit_time: str) -> Path:
                return snapshot.publish_response(
                    response(list(reversed(rows()))),
                    store=store,
                    acquisition_tool=FAKE_TOOL,
                    acquisition_toolchain=FAKE_TOOLCHAIN,
                    audit_time=audit_time,
                    before_publish=lambda _scratch, _target: barrier.wait(timeout=10),
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(publish, AUDIT_TIME),
                    executor.submit(publish, "2030-01-01T00:00:00Z"),
                ]
                targets = [future.result(timeout=20) for future in futures]
            self.assertEqual(targets[0], targets[1])
            published = [path for path in store.iterdir() if path.name != ".staging"]
            self.assertEqual(published, [targets[0]])
            self.assertFalse(any((store / ".staging").iterdir()))

    def test_existing_content_address_is_never_replaced_or_trusted_blindly(self) -> None:
        parsed = snapshot.parse_wrangler_output(response())
        bundle_id, _files = snapshot._bundle_files(
            parsed,
            acquisition_tool=FAKE_TOOL,
            acquisition_toolchain=FAKE_TOOLCHAIN,
            audit_time=AUDIT_TIME,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            store.mkdir(mode=0o700)
            store.chmod(0o700)
            target = store / bundle_id.removeprefix("sha256:")
            target.mkdir(mode=0o700)
            target.chmod(0o700)
            sentinel = target / "intruder"
            sentinel.write_text("preserve me")
            sentinel.chmod(0o644)
            with self.assertRaisesRegex(snapshot.D1SnapshotError, "member set"):
                snapshot.publish_response(
                    response(),
                    store=store,
                    acquisition_tool=FAKE_TOOL,
                    acquisition_toolchain=FAKE_TOOLCHAIN,
                    audit_time=AUDIT_TIME,
                )
            self.assertEqual(sentinel.read_text(), "preserve me")
            self.assertFalse(any((store / ".staging").iterdir()))

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires SIGKILL")
    def test_sigkill_before_atomic_publish_never_exposes_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            response_path = root / "response.json"
            marker = root / "ready"
            response_path.write_text(response())
            code = """
import signal
from pathlib import Path
import brain.acquire_d1_snapshot as snapshot
stdout = Path(%(response)r).read_text()
def pause(_scratch, _target):
    Path(%(marker)r).write_text('ready')
    signal.pause()
snapshot.publish_response(
    stdout,
    store=Path(%(store)r),
    acquisition_tool=%(tool)r,
    acquisition_toolchain=%(toolchain)r,
    audit_time=%(audit)r,
    before_publish=pause,
)
""" % {
                "response": str(response_path),
                "marker": str(marker),
                "store": str(store),
                "tool": FAKE_TOOL,
                "toolchain": FAKE_TOOLCHAIN,
                "audit": AUDIT_TIME,
            }
            child = subprocess.Popen([sys.executable, "-c", code], cwd=snapshot.ROOT)
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "child did not reach pre-publication barrier")
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            self.assertNotEqual(child.returncode, 0)
            published = [path for path in store.iterdir() if path.name != ".staging"]
            self.assertEqual(published, [])
            self.assertTrue(any((store / ".staging").iterdir()))
            self.assertEqual(len(snapshot.staging_orphans(store)), 1)

            target = snapshot.publish_response(
                response_path.read_text(),
                store=store,
                acquisition_tool=FAKE_TOOL,
                acquisition_toolchain=FAKE_TOOLCHAIN,
                audit_time=AUDIT_TIME,
            )
            self.assertTrue((target / "bundle.json").is_file())


if __name__ == "__main__":
    unittest.main()
