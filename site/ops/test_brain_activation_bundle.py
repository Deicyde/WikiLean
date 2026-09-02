#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import brain_activation_bundle as bundle
import brain_promote_release as promoter
from brain_public_baseline import PublicAssetBaseline, PublicAssetFile


RELEASE_ID = "sha256:" + "1" * 64
PRIOR_RELEASE_ID = "sha256:" + "2" * 64
BASELINE_ID = "sha256:" + "3" * 64


def _authority_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_json(path: Path, value: object, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.write_bytes(bundle._canonical_json_bytes(value))


def _run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ActivationFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        selected_git = shutil.which("git")
        if selected_git is None:
            raise RuntimeError("activation bundle tests require git")
        self.git = Path(selected_git).resolve(strict=True)
        self.promotion = self.root / "promotion"
        self.promotion.mkdir()
        _run("git", "init", "-b", "main", cwd=self.promotion)
        _run("git", "config", "user.email", "test@example.invalid", cwd=self.promotion)
        _run("git", "config", "user.name", "Test", cwd=self.promotion)

        self.baseline_asset_path = "robots.txt"
        self.baseline_asset_bytes = b"User-agent: *\nDisallow:\n"
        self.baseline_files = (
            PublicAssetFile(
                path=self.baseline_asset_path,
                sha256=hashlib.sha256(self.baseline_asset_bytes).hexdigest(),
                bytes=len(self.baseline_asset_bytes),
            ),
        )
        self.source_attestation = {
            "schema": bundle.SOURCE_ATTESTATION_SCHEMA,
            "files": [
                {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
                for item in self.baseline_files
            ],
        }
        self.source_path = self.promotion / bundle.SOURCE_ATTESTATION_PATH
        _write_json(self.source_path, self.source_attestation, pretty=False)
        (self.promotion / ".gitignore").write_text("ignored-releases/\n", encoding="utf-8")
        _run(
            "git",
            "add",
            bundle.SOURCE_ATTESTATION_PATH,
            ".gitignore",
            cwd=self.promotion,
        )
        _run("git", "-c", "commit.gpgsign=false", "commit", "-m", "attest", cwd=self.promotion)
        self.authority = _run("git", "rev-parse", "HEAD", cwd=self.promotion)

        self.build = self.root / "build"
        _run("git", "worktree", "add", "--detach", str(self.build), self.authority, cwd=self.promotion)

        self.release_root = self.root / "releases" / RELEASE_ID.removeprefix("sha256:")
        self.release_manifest_path = self.release_root / "release.json"
        semantic_roots = {
            path: "sha256:" + format(index, "064x")
            for index, path in enumerate(bundle.COMPATIBILITY_SEMANTIC_PATHS, 10)
        }
        self.baseline_semantic_roots = {
            path: "sha256:" + format(index + 100, "064x")
            for index, path in enumerate(bundle.COMPATIBILITY_SEMANTIC_PATHS)
        }
        self.page_bytes = b"<!doctype html><title>Activation fixture</title>"
        semantic_artifacts = [
            {
                "logical_name": path.replace("/", "."),
                "path": path,
                "media_type": "application/json",
                "sha256": format(index, "064x"),
                "bytes": index,
                "logical_format": "json",
                "logical_root": semantic_roots[path],
            }
            for index, path in enumerate(bundle.COMPATIBILITY_SEMANTIC_PATHS, 1)
        ]
        page_artifact = {
            "logical_name": "brain-page",
            "path": "site/out/brain.html",
            "media_type": "text/html",
            "sha256": hashlib.sha256(self.page_bytes).hexdigest(),
            "bytes": len(self.page_bytes),
            "logical_format": "opaque",
            "logical_root": None,
        }
        self.release_manifest = {
            "schema": bundle.RELEASE_SCHEMA,
            "profile": "wikilean-brain-complete-v1",
            "release_id": RELEASE_ID,
            "authority": {
                "git_commit": self.authority,
                "semantic_state_root": "sha256:" + "4" * 64,
                "through_changeset": None,
            },
            "source_set_root": "sha256:" + "5" * 64,
            "semantic_epoch": "brain-v2",
            "reducer": {
                "schedule": "nightly",
                "version": "1",
                "git_commit": self.authority,
                "configuration_sha256": "6" * 64,
                "environment_sha256": "7" * 64,
            },
            "artifacts": [*semantic_artifacts, page_artifact],
            "attestations": [],
            "compatible_overlay_generation_ids": [],
        }
        self.release_manifest_path.parent.mkdir(parents=True)
        self.release_manifest_path.write_bytes(_authority_json(self.release_manifest))
        page_path = self.release_root / "site" / "out" / "brain.html"
        page_path.parent.mkdir(parents=True)
        page_path.write_bytes(self.page_bytes)
        database = self.release_root / "brain" / "data" / "brain.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"sqlite-placeholder")

        self.semantic_baseline_root = (
            self.root / "releases" / PRIOR_RELEASE_ID.removeprefix("sha256:")
        )
        self.semantic_baseline_manifest_path = self.semantic_baseline_root / "release.json"
        self.semantic_baseline_manifest = json.loads(json.dumps(self.release_manifest))
        self.semantic_baseline_manifest["release_id"] = PRIOR_RELEASE_ID
        for artifact in self.semantic_baseline_manifest["artifacts"]:
            if artifact["path"] in self.baseline_semantic_roots:
                artifact["logical_root"] = self.baseline_semantic_roots[artifact["path"]]
        self.semantic_baseline_manifest_path.parent.mkdir(parents=True)
        self.semantic_baseline_manifest_path.write_bytes(
            _authority_json(self.semantic_baseline_manifest)
        )

        self.baseline_root = self.root / "baselines" / BASELINE_ID.removeprefix("sha256:")
        self.baseline_root.mkdir(parents=True)
        self.baseline_manifest_path = self.baseline_root / "manifest.json"
        self.baseline_manifest = {
            "schema": bundle.BASELINE_SCHEMA,
            "baseline_id": BASELINE_ID,
            "authority": {"git_commit": self.authority},
            "files": [
                {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
                for item in self.baseline_files
            ],
        }
        _write_json(self.baseline_manifest_path, self.baseline_manifest, pretty=False)
        (self.baseline_root / self.baseline_asset_path).write_bytes(
            self.baseline_asset_bytes
        )

        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        artifact_bytes = sum(item["bytes"] for item in self.release_manifest["artifacts"])
        self.release_result = {
            "release_id": RELEASE_ID,
            "release": RELEASE_ID.removeprefix("sha256:"),
            "root": str(self.release_root),
            "manifest": str(self.release_manifest_path),
            "artifact_count": len(self.release_manifest["artifacts"]),
            "byte_count": artifact_bytes,
            "reused": False,
        }
        self.metrics = {
            "schema": bundle.METRICS_SCHEMA,
            "measured_at": "2030-01-01T00:00:00+00:00",
            "ok": True,
            "identity": {
                "schema_version": 2,
                "release_id": RELEASE_ID,
                "release_id_source": str(self.release_manifest_path),
                "build_state": "complete",
                "snapshot_id": "base",
                "base_snapshot_id": "base",
                "projection_id": "projection",
                "snapshot_aliases_base": True,
                "snapshot_aliases_projection": False,
                "snapshot_id_alias": "base_snapshot_id",
            },
            "database": {
                "path": str(database),
                "file_bytes": len(b"sqlite-placeholder"),
                "application_id": bundle.BRAIN_SQLITE_APPLICATION_ID,
                "user_version": 2,
                "page_size_bytes": 4096,
                "page_count": 1,
                "allocated_bytes": 4096,
                "freelist_pages": 0,
                "freelist_bytes": 0,
                "used_pages": 1,
                "used_bytes": 4096,
                "freelist_fraction": 0.0,
                "journal_mode": "delete",
                "auto_vacuum": 0,
                "query_only": True,
                "immutable": True,
                "read_only": True,
            },
            "counts": {
                "tables": {
                    "nodes": 1,
                    "edges": 1,
                    "cells": 1,
                    "organ_owners": 1,
                    "synapses": 1,
                },
                "artifacts": {"nodes": 1, "edges": 1, "cells": 1, "synapses": 1},
                "edges_by_stream": {"main": 1},
            },
            "analyze": {
                "present": True,
                "entry_count": 1,
                "tables": ["nodes"],
                "indexes": ["nodes_type_label_idx"],
                "entries": [{"table": "nodes", "index": "nodes_type_label_idx", "stat": "1 1"}],
            },
            "checks": {
                "application_id": {
                    "ok": True,
                    "expected": bundle.BRAIN_SQLITE_APPLICATION_ID,
                    "actual": bundle.BRAIN_SQLITE_APPLICATION_ID,
                },
                "identity": {"ok": True},
                "quick_check": {
                    "ok": True,
                    "messages": ["ok"],
                    "duration_ms": 0.1,
                    "error_limit": 100,
                },
                "integrity_check": {
                    "ok": True,
                    "messages": ["ok"],
                    "duration_ms": 0.2,
                    "error_limit": 100,
                },
            },
            "queries": {
                name: {
                    "status": "ok",
                    "sample_key": "fixture",
                    "limit": 100,
                    "iterations": 5,
                    "warmup_iterations": 1,
                    "rows_returned": 1,
                    "latency_ms": {
                        "min": 0.01,
                        "p50": 0.02,
                        "p95": 0.03,
                        "mean": 0.02,
                        "max": 0.03,
                    },
                    "plan": [{"id": 1, "parent": 0, "detail": "SEARCH fixture"}],
                    "plan_summary": {
                        "expected_indexes": (
                            []
                            if name == "owner_lookup"
                            else ["edges_src_kind_idx", "edges_dst_kind_idx"]
                            if name == "edge_neighborhood"
                            else ["synapses_src_idx", "synapses_dst_idx"]
                        ),
                        "used_expected_indexes": (
                            []
                            if name == "owner_lookup"
                            else ["edges_src_kind_idx", "edges_dst_kind_idx"]
                            if name == "edge_neighborhood"
                            else ["synapses_src_idx", "synapses_dst_idx"]
                        ),
                        "all_expected_indexes_used": True,
                        "base_table_full_scans": [],
                    },
                }
                for name in (
                    "owner_lookup",
                    "edge_neighborhood",
                    "synapse_neighborhood",
                )
            },
            "warnings": [],
            "duration_ms": 1.25,
            "max_rss_bytes": 1024,
        }
        self.public_result = self._public_result(
            include_baseline=False,
            public_dir=self.build / "wiki" / "public",
        )
        self._materialize_shadow_public()
        required = list(bundle.COMPATIBILITY_SEMANTIC_PATHS)
        self.semantic_diff = {
            "schema": bundle.SEMANTIC_DIFF_SCHEMA,
            "from": {
                "kind": "release-manifest",
                "path": str(self.semantic_baseline_manifest_path),
                "release_id": PRIOR_RELEASE_ID,
            },
            "to": {
                "kind": "release-manifest",
                "path": str(self.release_manifest_path),
                "release_id": RELEASE_ID,
            },
            "coverage": {
                "required": required,
                "from": {"present": required, "missing": [], "complete": True},
                "to": {"present": required, "missing": [], "complete": True},
                "compared": required,
                "complete": True,
            },
            "semantic_artifacts": {
                path: {
                    "from": self.baseline_semantic_roots[path],
                    "to": semantic_roots[path],
                    "compared": True,
                    "different": path not in {
                        "brain/data/cells.jsonl",
                        "brain/data/frontier.jsonl",
                    },
                }
                for path in required
            },
            "nodes": {"added": [{"id": "Q1"}], "removed": [], "changed": []},
            "edges": {
                "added": [],
                "removed": [],
                "changed": [],
                "provenance_only": [],
                "grouped_by_source_kind": [],
                "compared_artifacts": [
                    "brain/data/edges.jsonl",
                    "brain/data/edges_links.jsonl",
                ],
            },
            "snippets": {
                "added": [],
                "removed": [],
                "changed": [],
                "grouped_by_field_source": [],
            },
            "cells": {"added": [], "removed": [], "changed": []},
            "organ_membership": {
                "added": [],
                "removed": [],
                "moved": [],
                "changed": [],
                "provenance_only": [],
                "splits": [],
                "merges": [],
            },
            "frontier": {"added": [], "removed": [], "changed": []},
            "synapses": {
                **{
                    key: value
                    for key, value in {
                        "from": self.baseline_semantic_roots["brain/data/synapses.jsonl"],
                        "to": semantic_roots["brain/data/synapses.jsonl"],
                        "compared": True,
                        "different": True,
                    }.items()
                }
            },
            "frontier_graph": {
                "from": self.baseline_semantic_roots["brain/data/frontier_graph.json"],
                "to": semantic_roots["brain/data/frontier_graph.json"],
                "compared": True,
                "different": True,
            },
        }
        self.semantic_diff["summary"] = bundle.semantic_diff_tool.summarize_report(
            self.semantic_diff
        )
        self.semantic_diff["different"] = bundle.semantic_diff_tool.summary_has_differences(
            self.semantic_diff["summary"]
        )
        self.build_context = {
            "schema": bundle.BUILD_CONTEXT_SCHEMA,
            "authority_git_commit": self.authority,
            "build_worktree": {
                "root": str(self.build),
                "head": self.authority,
                "branch": "detached",
                "clean": False,
            },
            "promotion_worktree": {
                "root": str(self.promotion),
                "head": self.authority,
                "branch": "main",
                "clean": True,
            },
        }
        selected_git = str(self.git)
        selected_node = "/usr/bin/node"
        selected_npm = "/usr/bin/npm"
        selected_python = "/usr/bin/python3"

        def command_evidence(
            name: str,
            argv: list[str],
            cwd: Path,
            stdout: str,
            *,
            environment_overrides: dict[str, str] | None = None,
        ) -> dict[str, object]:
            stdout_bytes = stdout.encode("utf-8")
            stderr_bytes = b""
            return {
                "name": name,
                "argv": argv,
                "cwd": str(cwd),
                "environment_overrides": environment_overrides or {},
                "returncode": 0,
                "stdout": stdout,
                "stdout_bytes": len(stdout_bytes),
                "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                "stderr": "",
                "stderr_bytes": 0,
                "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            }

        self.ci_evidence = {
            "schema": bundle.CI_EVIDENCE_SCHEMA,
            "ok": True,
            "authority": {"git_commit": self.authority, "branch": "main"},
            "repo_root": str(self.promotion),
            "environment": {
                "policy": "wikilean.brain-activation-ci-environment/v2",
                "credentials_inherited": False,
                "git_overrides_inherited": False,
                "deployment_enabled": False,
                "caller_path_inherited": False,
                "tool_paths_pinned": True,
            },
            "tools": {
                "git": {
                    "path": selected_git,
                    "version": "git version 2.51.0",
                    "probe": command_evidence(
                        "git_version",
                        [selected_git, "--version"],
                        self.promotion,
                        "git version 2.51.0\n",
                    ),
                },
                "node": {
                    "path": selected_node,
                    "version": "v22.23.2",
                    "probe": command_evidence(
                        "node_version",
                        [selected_node, "--version"],
                        self.promotion / "wiki",
                        "v22.23.2\n",
                    ),
                },
                "npm": {
                    "path": selected_npm,
                    "version": "10.9.8",
                    "probe": command_evidence(
                        "npm_version",
                        [selected_npm, "--version"],
                        self.promotion / "wiki",
                        "10.9.8\n",
                    ),
                },
                "python": {
                    "path": selected_python,
                    "version": "Python 3.12.14",
                    "probe": command_evidence(
                        "python_version",
                        [selected_python, "--version"],
                        self.promotion,
                        "Python 3.12.14\n",
                    ),
                },
            },
            "checkout": {
                "clean_before": True,
                "clean_after": True,
                "head_before": self.authority,
                "head_after": self.authority,
                "main_before": self.authority,
                "main_after": self.authority,
            },
            "checks": [
                command_evidence(
                    "npm_ci",
                    [selected_npm, "ci"],
                    self.promotion / "wiki",
                    "installed\n",
                ),
                command_evidence(
                    "worker_ci",
                    [selected_npm, "run", "test:ci"],
                    self.promotion / "wiki",
                    "worker green\n",
                ),
                command_evidence(
                    "python_ci",
                    ["./scripts/ci-python.sh"],
                    self.promotion,
                    "python green\n",
                    environment_overrides={"PYTHON": selected_python},
                ),
            ],
        }

        def tree_inventory(root: Path, digest: str) -> dict[str, object]:
            return {
                "schema": "wikilean.file-tree-inventory/v1",
                "root": str(root),
                "objects": 1,
                "bytes": 1,
                "sha256": digest * 64,
            }

        attempt_id = "20300101T000000Z-111111111111-deadbeef00"
        promoter_public_dir = self.root / "sealed-public"
        promoter_public_result = self._public_result(
            include_baseline=True,
            public_dir=promoter_public_dir,
        )
        self.dry_run = {
            "schema": bundle.DRY_RUN_SCHEMA,
            "ok": True,
            "attempt_id": attempt_id,
            "proposed_intent": {
                "requested_release_id": RELEASE_ID,
                "release_root": str(self.release_root),
                "release_manifest_sha256": hashlib.sha256(
                    self.release_manifest_path.read_bytes()
                ).hexdigest(),
                "release_tree": bundle._inventory_tree(self.release_root),
                "authority_commit": self.authority,
                "reducer_commit": self.authority,
                "retained_release": None,
                "public_baseline": {
                    "baseline_id": BASELINE_ID,
                    "root": str(self.baseline_root),
                    "manifest": str(self.baseline_manifest_path),
                    "manifest_sha256": hashlib.sha256(
                        self.baseline_manifest_path.read_bytes()
                    ).hexdigest(),
                    "authority_commit": self.authority,
                    "files": len(self.baseline_files),
                    "bytes": sum(item.bytes for item in self.baseline_files),
                },
                "public_tree": tree_inventory(promoter_public_dir, "b"),
                "public_result": promoter_public_result,
                "staged_selector": {
                    "sha256": "c" * 64,
                    "release_id": RELEASE_ID,
                    "previous_release_id": None,
                    "audited_at": "2030-01-01T00:00:00Z",
                },
                "worker_bundle": {
                    "tree": tree_inventory(self.root / "sealed-bundle", "d"),
                    "entry": str(self.root / "sealed-bundle" / "index.js"),
                    "config": str(self.root / "sealed-config" / "wrangler.jsonc"),
                    "config_sha256": "e" * 64,
                    "node_version": "v22.23.2",
                    "wrangler_version": "4.120.0",
                },
                "audited_at": "2030-01-01T00:00:00Z",
                "base_url": bundle.PRODUCTION_ORIGIN,
                "trust_source": "certifi:test",
                "predeploy": {
                    "deployment_id": "11111111-1111-1111-1111-111111111111",
                    "version_id": "22222222-2222-2222-2222-222222222222",
                    "status_sha256": "f" * 64,
                    "selector_status": 404,
                    "selector_sha256": hashlib.sha256(b"").hexdigest(),
                    "release_id": None,
                    "previous_release_id": None,
                    "audited_at": None,
                },
                "planned": {
                    "tag": f"brain-{RELEASE_ID.removeprefix('sha256:')[:12]}-{attempt_id[-10:]}",
                    "message": f"Brain release {RELEASE_ID} attempt {attempt_id}",
                    "command_timeout_seconds": "900",
                },
                "approval_note": None,
                "first_deploy_exception": True,
                "first_deploy_approval": "fixture approval",
                "history": {
                    "deployments": {
                        "sha256": "1" * 64,
                        "bytes": 2,
                        "entries": 0,
                    },
                    "versions": {
                        "sha256": "2" * 64,
                        "bytes": 2,
                        "entries": 0,
                    },
                },
            },
            "production_mutated": False,
        }
        self._attach_retained_artifacts()
        self.paths = {
            "candidate_release_manifest": self.release_manifest_path,
            "semantic_baseline_manifest": self.semantic_baseline_manifest_path,
            "public_baseline_manifest": self.baseline_manifest_path,
            "source_attestation": self.source_path,
            "release_result": self.evidence / "release-result.json",
            "release_metrics": self.evidence / "release-metrics.json",
            "shadow_public_result": self.evidence / "shadow-public-result.json",
            "semantic_diff": self.evidence / "semantic-diff.json",
            "promoter_dry_run": self.evidence / "promoter-dry-run.json",
            "build_context": self.evidence / "build-context.json",
        }
        self.sync()
        self.store = self.root / "activation-store"

    def _attach_retained_artifacts(self) -> None:
        work = self.root / "promoter-work"
        if work.exists() or work.is_symlink():
            promoter.remove_sealed_tree(work)
        public = work / "public"
        worker = work / "worker"
        shutil.copytree(Path(self.public_result["public_dir"]), public)
        worker.mkdir(parents=True)
        worker_entry = worker / "index.js"
        worker_entry.write_text("export default {};\n", encoding="utf-8")
        config = work / "wrangler.jsonc"
        config.write_text("{}\n", encoding="utf-8")
        public_inventory = promoter.inventory_tree(public)
        worker_inventory = promoter.inventory_tree(worker)
        promoter.seal_tree_read_only(public)
        promoter.seal_tree_read_only(worker)
        config.chmod(0o400)

        selector_body = b""
        status_body = (
            b'{"id":"11111111-1111-1111-1111-111111111111",'
            b'"versions":[{"percentage":100,'
            b'"version_id":"22222222-2222-2222-2222-222222222222"}]}\n'
        )
        predeploy = promoter.parse_deployment_status(status_body)
        history_raw = {"deployments": b"[]\n", "versions": b"[]\n"}
        history = {
            key: {
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "entries": 0,
            }
            for key, body in history_raw.items()
        }
        baseline = PublicAssetBaseline(
            root=self.baseline_root,
            manifest_path=self.baseline_manifest_path,
            baseline_id=BASELINE_ID,
            baseline_hex=BASELINE_ID.removeprefix("sha256:"),
            authority_git_commit=self.authority,
            files=self.baseline_files,
            total_bytes=sum(item.bytes for item in self.baseline_files),
        )

        def rebased_public_result(public_dir: Path) -> dict[str, object]:
            result = json.loads(json.dumps(self.public_result))
            result["public_dir"] = str(public_dir)
            result["public_baseline"] = {
                "schema": bundle.BASELINE_SCHEMA,
                "baseline_id": BASELINE_ID,
                "authority_commit": self.authority,
                "root": str(self.baseline_root),
                "files": len(self.baseline_files),
                "bytes": sum(item.bytes for item in self.baseline_files),
            }
            result["brain"]["destination"] = str(public_dir / "assets" / "brain")
            result["brain"]["brain_page"]["destination"] = str(
                public_dir / "brain.html"
            )
            return result

        candidate = promoter.ReleaseInfo(
            RELEASE_ID,
            RELEASE_ID.removeprefix("sha256:"),
            self.release_root,
            self.release_manifest_path,
            hashlib.sha256(self.release_manifest_path.read_bytes()).hexdigest(),
            self.authority,
            self.authority,
            promoter.inventory_tree(self.release_root),
        )
        audited_at = self.dry_run["proposed_intent"]["audited_at"]
        staged_body = (public / "assets" / "brain" / "current.json").read_bytes()
        staged_selector = promoter.SelectorState(
            200,
            hashlib.sha256(staged_body).hexdigest(),
            staged_body,
            RELEASE_ID,
            None,
            None,
            audited_at,
        )
        prepared = promoter.PreparedPromotion(
            self.dry_run["attempt_id"],
            audited_at,
            self.dry_run["proposed_intent"]["planned"]["tag"],
            self.dry_run["proposed_intent"]["planned"]["message"],
            candidate,
            None,
            None,
            baseline,
            promoter.SelectorState(
                404,
                hashlib.sha256(selector_body).hexdigest(),
                selector_body,
                None,
                None,
                None,
                None,
            ),
            predeploy,
            status_body,
            status_body,
            public,
            public_inventory,
            rebased_public_result(public),
            staged_selector,
            worker,
            worker_entry,
            worker_inventory,
            config,
            hashlib.sha256(config.read_bytes()).hexdigest(),
            "v22.23.2",
            "4.120.0",
            "certifi:test",
            history,
            history_raw,
        )
        receipt_root = self.root / "receipts"
        receipt_root.mkdir(exist_ok=True)
        retained = promoter.retain_dry_run_artifacts(
            prepared,
            self.root / "retained-dry-runs",
            repo_root=self.promotion,
            receipt_root=receipt_root,
        )
        intent = self.dry_run["proposed_intent"]
        intent["public_tree"] = promoter.inventory_tree(retained.public_dir)
        intent["public_result"] = rebased_public_result(retained.public_dir)
        intent["worker_bundle"]["tree"] = promoter.inventory_tree(retained.worker_dir)
        intent["worker_bundle"]["entry"] = str(retained.worker_entry)
        intent["worker_bundle"]["config"] = str(retained.config)
        intent["worker_bundle"]["config_sha256"] = hashlib.sha256(
            retained.config.read_bytes()
        ).hexdigest()
        intent["history"] = history
        intent["predeploy"]["status_sha256"] = predeploy.raw_sha256
        intent["staged_selector"]["sha256"] = staged_selector.body_sha256
        intent["retained_artifacts"] = retained.reference()

    def _public_result(
        self, *, include_baseline: bool, public_dir: Path
    ) -> dict[str, object]:
        page_digest = hashlib.sha256(self.page_bytes).hexdigest()
        return {
            "schema": bundle.PUBLIC_RESULT_SCHEMA,
            "public_dir": str(public_dir),
            "mathlib_declarations": 1,
            "public_baseline": (
                {
                    "schema": bundle.BASELINE_SCHEMA,
                    "baseline_id": BASELINE_ID,
                    "authority_commit": self.authority,
                    "root": str(self.baseline_root),
                    "files": len(self.baseline_files),
                    "bytes": sum(item.bytes for item in self.baseline_files),
                }
                if include_baseline
                else None
            ),
            "brain": {
                "schema": bundle.PUBLIC_STAGE_SCHEMA,
                "release_id": RELEASE_ID,
                "release": RELEASE_ID.removeprefix("sha256:"),
                "previous_release_id": None,
                "retained_release_ids": [RELEASE_ID],
                "destination": str(public_dir / "assets" / "brain"),
                "objects": 3,
                "bytes": len(self.page_bytes) + len(self.release_manifest_path.read_bytes()) + 1,
                "largest_file_bytes": max(
                    len(self.page_bytes), len(self.release_manifest_path.read_bytes())
                ),
                "copy_buffer_bytes": bundle.COPY_BUFFER_BYTES,
                "duration_ms": 0.5,
                "max_rss_bytes": 1024,
                "free_bytes_before": 100,
                "free_bytes_after": 99,
                "brain_page": {
                    "destination": str(public_dir / "brain.html"),
                    "bytes": len(self.page_bytes),
                    "sha256": page_digest,
                },
                "warnings": [],
            },
            "duration_ms": 1.0,
            "max_rss_bytes": 2048,
        }

    def _materialize_shadow_public(self) -> None:
        public_dir = Path(self.public_result["public_dir"])
        destination = Path(self.public_result["brain"]["destination"])
        namespace = destination / "releases" / RELEASE_ID.removeprefix("sha256:")
        namespace.mkdir(parents=True)
        (public_dir / self.baseline_asset_path).write_bytes(self.baseline_asset_bytes)
        (namespace / "release.json").write_bytes(self.release_manifest_path.read_bytes())
        selector = {
            "schema": "wikilean.release-selector/v1",
            "release_id": RELEASE_ID,
            "release": RELEASE_ID.removeprefix("sha256:"),
            "manifest": (
                "/assets/brain/releases/"
                + RELEASE_ID.removeprefix("sha256:")
                + "/release.json"
            ),
            "audited_at": "2030-01-01T00:00:00Z",
        }
        (destination / "current.json").write_text(
            json.dumps(selector, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        (public_dir / "brain.html").write_bytes(self.page_bytes)
        assets = bundle._scan_regular_tree(destination, "fixture shadow tree")
        sizes = [size for size, _ in assets.values()] + [len(self.page_bytes)]
        brain = self.public_result["brain"]
        brain["objects"] = len(sizes)
        brain["bytes"] = sum(sizes)
        brain["largest_file_bytes"] = max(sizes)

    def sync(self) -> None:
        values = {
            "release_result": self.release_result,
            "release_metrics": self.metrics,
            "shadow_public_result": self.public_result,
            "semantic_diff": self.semantic_diff,
            "promoter_dry_run": self.dry_run,
            "build_context": self.build_context,
        }
        for kind, value in values.items():
            _write_json(self.paths[kind], value, pretty=True)

    @contextmanager
    def verifiers(self):
        baseline = PublicAssetBaseline(
            root=self.baseline_root,
            manifest_path=self.baseline_manifest_path,
            baseline_id=BASELINE_ID,
            baseline_hex=BASELINE_ID.removeprefix("sha256:"),
            authority_git_commit=self.authority,
            files=self.baseline_files,
            total_bytes=sum(item.bytes for item in self.baseline_files),
        )

        def validate_release(value):
            return value

        def verify_baseline(root, repo, **kwargs):
            if Path(root) != self.baseline_root or Path(repo) != self.promotion:
                raise AssertionError("wrong baseline verification roots")
            if kwargs.get("expected_baseline_id") != BASELINE_ID:
                raise AssertionError("wrong expected baseline")
            if kwargs.get("expected_authority_git_commit") != self.authority:
                raise AssertionError("wrong expected authority")
            if kwargs.get("git_executable") != self.git:
                raise AssertionError("wrong approved Git executable")
            return baseline

        with mock.patch.object(bundle, "validate_release_manifest", side_effect=validate_release), mock.patch.object(
            bundle, "verify_release_files", return_value={}
        ), mock.patch.object(
            bundle,
            "validate_public_baseline_manifest",
            return_value=(BASELINE_ID, self.authority, self.baseline_files),
        ), mock.patch.object(
            bundle, "verify_public_baseline", side_effect=verify_baseline
        ), mock.patch.object(
            bundle.semantic_diff_tool,
            "compare_paths",
            return_value=self.semantic_diff,
        ), mock.patch.object(
            bundle.measure_store_tool,
            "measure_database",
            return_value=self.metrics,
        ):
            with mock.patch.object(bundle, "REPO_ROOT", self.promotion):
                yield

    def freeze(self) -> bundle.ActivationBundle:
        with self.verifiers():
            return bundle.freeze_activation_bundle(
                self.paths,
                self.store,
                ci_evidence=self.ci_evidence,
                expected_semantic_baseline_id=PRIOR_RELEASE_ID,
                git=self.git,
            )

    def freeze_activation_to(self, store: Path) -> bundle.ActivationBundle:
        with self.verifiers():
            return bundle.freeze_activation_bundle(
                self.paths,
                store,
                ci_evidence=self.ci_evidence,
                expected_semantic_baseline_id=PRIOR_RELEASE_ID,
                git=self.git,
            )

    def verify(self, root: Path) -> bundle.ActivationBundle:
        with self.verifiers():
            return bundle.verify_activation_bundle(root)

    def close(self) -> None:
        self.temporary.cleanup()


class BrainActivationBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ActivationFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_happy_path_freezes_canonical_read_only_exact_bundle(self):
        frozen = self.fixture.freeze()
        self.assertEqual(frozen.release_id, RELEASE_ID)
        self.assertEqual(frozen.semantic_baseline_release_id, PRIOR_RELEASE_ID)
        self.assertEqual(frozen.baseline_id, BASELINE_ID)
        self.assertEqual(stat.S_IMODE(frozen.root.stat().st_mode), 0o555)
        self.assertEqual(
            {path.name for path in frozen.root.iterdir()},
            {bundle.MANIFEST_NAME, *bundle.EVIDENCE_BY_PATH},
        )
        for path in frozen.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(path.stat().st_nlink, 1)
        release_result = (frozen.root / "release-result.json").read_bytes()
        self.assertEqual(
            release_result,
            bundle._canonical_json_bytes(self.fixture.release_result),
        )
        verified = self.fixture.verify(frozen.root)
        self.assertEqual(verified.bundle_id, frozen.bundle_id)
        semantic = json.loads((frozen.root / "semantic-diff.json").read_bytes())
        cell = semantic["semantic_artifacts"]["brain/data/cells.jsonl"]
        self.assertNotEqual(cell["from"], cell["to"])
        self.assertFalse(cell["different"])

    def test_refreeze_reuses_identical_bundle(self):
        first = self.fixture.freeze()
        second = self.fixture.freeze()
        self.assertEqual(first.bundle_id, second.bundle_id)
        self.assertEqual(first.root, second.root)

    def test_verify_remains_valid_after_build_worktree_is_removed(self):
        frozen = self.fixture.freeze()
        _run(
            "git",
            "worktree",
            "remove",
            "--force",
            str(self.fixture.build),
            cwd=self.fixture.promotion,
        )
        (self.fixture.promotion / "later-dirty.txt").write_text(
            "post-freeze state must not affect verification", encoding="utf-8"
        )
        shutil.rmtree(self.fixture.release_root)
        shutil.rmtree(self.fixture.semantic_baseline_root)
        shutil.rmtree(self.fixture.baseline_root)
        verified = self.fixture.verify(frozen.root)
        self.assertEqual(verified.bundle_id, frozen.bundle_id)

    def test_verify_requires_recorded_retained_artifact_root(self):
        frozen = self.fixture.freeze()
        retained_root = Path(
            self.fixture.dry_run["proposed_intent"]["retained_artifacts"]["root"]
        )
        promoter.remove_sealed_tree(retained_root)
        with self.assertRaisesRegex(bundle.BundleValidationError, "retained artifact"):
            self.fixture.verify(frozen.root)

    def test_rejects_release_identity_mismatch(self):
        self.fixture.metrics["identity"]["release_id"] = PRIOR_RELEASE_ID
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "metrics"):
            self.fixture.freeze()

    def test_rejects_baseline_identity_mismatch(self):
        self.fixture.dry_run["proposed_intent"]["public_result"]["public_baseline"][
            "baseline_id"
        ] = "sha256:" + "9" * 64
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "baseline"):
            self.fixture.freeze()

    def test_rejects_authority_reducer_mismatch(self):
        self.fixture.release_manifest["reducer"]["git_commit"] = "a" * 40
        self.fixture.release_manifest_path.write_bytes(_authority_json(self.fixture.release_manifest))
        self.fixture.dry_run["proposed_intent"]["release_manifest_sha256"] = hashlib.sha256(
            self.fixture.release_manifest_path.read_bytes()
        ).hexdigest()
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "authority and reducer"):
            self.fixture.freeze()

    def test_rejects_candidate_self_diff_as_semantic_baseline(self):
        paths = dict(self.fixture.paths)
        paths["semantic_baseline_manifest"] = self.fixture.release_manifest_path
        with self.fixture.verifiers():
            with self.assertRaisesRegex(bundle.BundleValidationError, "must differ"):
                bundle.freeze_activation_bundle(
                    paths,
                    self.fixture.store,
                    ci_evidence=self.fixture.ci_evidence,
                    expected_semantic_baseline_id=RELEASE_ID,
                    git=self.fixture.git,
                )

    def test_rejects_partial_semantic_coverage(self):
        self.fixture.semantic_diff["coverage"]["compared"] = list(
            bundle.COMPATIBILITY_SEMANTIC_PATHS[:-1]
        )
        self.fixture.semantic_diff["coverage"]["complete"] = False
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "coverage"):
            self.fixture.freeze()

    def test_rejects_data_directory_semantic_comparison(self):
        self.fixture.semantic_diff["from"]["kind"] = "data-directory"
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "release manifests"):
            self.fixture.freeze()

    def test_rejects_semantic_target_root_mismatch(self):
        path = bundle.COMPATIBILITY_SEMANTIC_PATHS[0]
        self.fixture.semantic_diff["semantic_artifacts"][path]["to"] = "sha256:" + "f" * 64
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "target root"):
            self.fixture.freeze()

    def test_rejects_semantic_source_root_mismatch(self):
        path = bundle.COMPATIBILITY_SEMANTIC_PATHS[0]
        self.fixture.semantic_diff["semantic_artifacts"][path]["from"] = (
            "sha256:" + "e" * 64
        )
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "source root"):
            self.fixture.freeze()

    def test_rejects_semantic_source_release_id_mismatch(self):
        self.fixture.semantic_diff["from"]["release_id"] = "sha256:" + "d" * 64
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "semantic diff source"):
            self.fixture.freeze()

    def test_rejects_production_mutated_dry_run(self):
        self.fixture.dry_run["production_mutated"] = True
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "non-mutating"):
            self.fixture.freeze()

    def test_rejects_incomplete_promoter_intent(self):
        self.fixture.dry_run["proposed_intent"].pop("worker_bundle")
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "worker_bundle"):
            self.fixture.freeze()

    def test_rejects_staged_selector_digest_not_bound_to_retained_public_bytes(self):
        self.fixture.dry_run["proposed_intent"]["staged_selector"]["sha256"] = "c" * 64
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "staged selector bytes"):
            self.fixture.freeze()

    def test_rejects_missing_retained_non_brain_baseline_file(self):
        public_dir = Path(self.fixture.public_result["public_dir"])
        (public_dir / self.fixture.baseline_asset_path).unlink()
        self.fixture._attach_retained_artifacts()
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "non-Brain public tree"):
            self.fixture.freeze()

    def test_rejects_changed_retained_non_brain_baseline_file(self):
        public_dir = Path(self.fixture.public_result["public_dir"])
        (public_dir / self.fixture.baseline_asset_path).write_bytes(b"changed\n")
        self.fixture._attach_retained_artifacts()
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "non-Brain public tree"):
            self.fixture.freeze()

    def test_rejects_extra_retained_non_brain_file(self):
        public_dir = Path(self.fixture.public_result["public_dir"])
        extra = public_dir / "assets" / "unexpected.js"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"unexpected\n")
        self.fixture._attach_retained_artifacts()
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "non-Brain public tree"):
            self.fixture.freeze()

    def test_rejects_incomplete_metrics_checks_and_queries(self):
        self.fixture.metrics["checks"].pop("integrity_check")
        self.fixture.metrics["queries"] = {}
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "integrity_check"):
            self.fixture.freeze()

    def test_rejects_ci_evidence_from_another_authority(self):
        self.fixture.ci_evidence["authority"]["git_commit"] = "f" * 40
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "activation CI"):
            self.fixture.freeze()

    def test_rejects_ci_evidence_for_a_different_git_executable(self):
        recorded_git = "/approved/other/git"
        self.fixture.ci_evidence["tools"]["git"]["path"] = recorded_git
        self.fixture.ci_evidence["tools"]["git"]["probe"]["argv"] = [
            recorded_git,
            "--version",
        ]
        with self.assertRaisesRegex(bundle.BundleValidationError, "Git executable differs"):
            self.fixture.freeze()

    def test_rejects_missing_shadow_public_output(self):
        shutil.rmtree(Path(self.fixture.public_result["public_dir"]))
        self.fixture.build_context["build_worktree"]["clean"] = True
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "shadow public"):
            self.fixture.freeze()

    def test_freezes_fresh_measurement_instead_of_unverified_observation_fields(self):
        source = self.fixture.paths["release_metrics"]
        raw = source.read_text(encoding="utf-8")
        exact = "0.12345678901234567890123456789"
        source.write_text(raw.replace("1.25", exact, 1), encoding="utf-8")
        frozen = self.fixture.freeze()
        frozen_metrics = json.loads((frozen.root / "release-metrics.json").read_bytes())
        self.assertEqual(frozen_metrics["duration_ms"], self.fixture.metrics["duration_ms"])
        self.assertNotIn(exact.encode(), (frozen.root / "release-metrics.json").read_bytes())

    def test_rejects_weaker_than_nightly_metric_probe_settings(self):
        self.fixture.metrics["queries"]["owner_lookup"]["iterations"] = 1
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "activation probe settings"):
            self.fixture.freeze()

    def test_rejects_same_worktree(self):
        context = dict(self.fixture.build_context)
        context["build_worktree"] = dict(context["promotion_worktree"])
        with self.assertRaisesRegex(bundle.BundleValidationError, "distinct"):
            bundle._validate_build_context(
                context,
                self.fixture.authority,
                inspect_external=True,
                git=self.fixture.git,
            )

    def test_rejects_overlapping_worktrees(self):
        context = dict(self.fixture.build_context)
        with mock.patch.object(
            bundle,
            "_inspect_worktree",
            side_effect=[
                (self.fixture.promotion, self.fixture.authority, "main", True),
                (self.fixture.promotion / "nested", self.fixture.authority, "detached", True),
            ],
        ):
            with self.assertRaisesRegex(bundle.BundleValidationError, "non-overlapping"):
                bundle._validate_build_context(
                    context,
                    self.fixture.authority,
                    inspect_external=True,
                    git=self.fixture.git,
                )

    def test_rejects_dirty_promotion_worktree_even_if_context_claims_clean(self):
        (self.fixture.promotion / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(bundle.BundleValidationError, "disagrees with Git"):
            self.fixture.freeze()

    def test_rejects_bundle_tool_running_outside_declared_promotion_worktree(self):
        with mock.patch.object(bundle, "REPO_ROOT", self.fixture.root):
            with self.assertRaisesRegex(bundle.BundleValidationError, "must run"):
                bundle._validate_build_context(
                    self.fixture.build_context,
                    self.fixture.authority,
                    inspect_external=True,
                    git=self.fixture.git,
                )

    def test_rejects_main_ref_that_differs_from_authority(self):
        _run("git", "checkout", "--detach", self.fixture.authority, cwd=self.fixture.promotion)
        tree = _run("git", "write-tree", cwd=self.fixture.promotion)
        other = _run(
            "git",
            "-c",
            "commit.gpgsign=false",
            "commit-tree",
            tree,
            "-p",
            self.fixture.authority,
            "-m",
            "other",
            cwd=self.fixture.promotion,
        )
        _run("git", "update-ref", "refs/heads/main", other, cwd=self.fixture.promotion)
        self.fixture.build_context["promotion_worktree"]["branch"] = "detached"
        with mock.patch.object(bundle, "REPO_ROOT", self.fixture.promotion):
            with self.assertRaisesRegex(bundle.BundleValidationError, "refs/heads/main"):
                bundle._validate_build_context(
                    self.fixture.build_context,
                    self.fixture.authority,
                    inspect_external=True,
                    git=self.fixture.git,
                )

    def test_rejects_merge_in_progress(self):
        git_dir = Path(_run("git", "rev-parse", "--absolute-git-dir", cwd=self.fixture.promotion))
        (git_dir / "MERGE_HEAD").write_text(self.fixture.authority + "\n", encoding="ascii")
        with mock.patch.object(bundle, "REPO_ROOT", self.fixture.promotion):
            with self.assertRaisesRegex(bundle.BundleValidationError, "merge or rebase"):
                bundle._validate_build_context(
                    self.fixture.build_context,
                    self.fixture.authority,
                    inspect_external=True,
                    git=self.fixture.git,
                )

    def test_context_builder_records_the_verified_worktrees(self):
        with mock.patch.object(bundle, "REPO_ROOT", self.fixture.promotion):
            context = bundle.create_build_context(
                self.fixture.build, self.fixture.promotion, git=self.fixture.git
            )
        self.assertEqual(context, self.fixture.build_context)

    def test_context_builder_ignores_caller_path_git_shadow(self):
        hostile = self.fixture.root / "hostile-bin"
        hostile.mkdir()
        fake_git = hostile / "git"
        fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        fake_git.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(hostile)}):
            with mock.patch.object(bundle, "REPO_ROOT", self.fixture.promotion):
                context = bundle.create_build_context(
                    self.fixture.build,
                    self.fixture.promotion,
                    git=self.fixture.git,
                )
        self.assertEqual(context, self.fixture.build_context)

    def test_rejects_output_store_inside_worktree_without_creating_it(self):
        store = self.fixture.promotion / "forbidden-store"
        with self.assertRaisesRegex(bundle.BundleValidationError, "outside"):
            self.fixture.freeze_activation_to(store)
        self.assertFalse(store.exists())

    def test_rejects_candidate_release_inside_promotion_worktree(self):
        nested = (
            self.fixture.promotion
            / "ignored-releases"
            / RELEASE_ID.removeprefix("sha256:")
        )
        shutil.copytree(self.fixture.release_root, nested)
        self.fixture.paths["candidate_release_manifest"] = nested / "release.json"
        self.fixture.release_result["root"] = str(nested)
        self.fixture.release_result["manifest"] = str(nested / "release.json")
        self.fixture.metrics["database"]["path"] = str(
            nested / "brain" / "data" / "brain.sqlite3"
        )
        self.fixture.metrics["identity"]["release_id_source"] = str(nested / "release.json")
        self.fixture.semantic_diff["to"]["path"] = str(nested / "release.json")
        intent = self.fixture.dry_run["proposed_intent"]
        intent["release_root"] = str(nested)
        intent["release_tree"] = bundle._inventory_tree(nested)
        self.fixture.sync()
        with self.assertRaisesRegex(bundle.BundleValidationError, "outside the promotion"):
            self.fixture.freeze()

    def test_rejects_tampered_evidence(self):
        frozen = self.fixture.freeze()
        target = frozen.root / "release-metrics.json"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes().replace(b'"ok":true', b'"ok":false'))
        target.chmod(0o444)
        with self.assertRaisesRegex(bundle.BundleValidationError, "digest/size"):
            self.fixture.verify(frozen.root)

    def test_rejects_tampered_semantic_baseline_manifest(self):
        frozen = self.fixture.freeze()
        target = frozen.root / "semantic-baseline-release.json"
        target.chmod(0o644)
        target.write_bytes(
            target.read_bytes().replace(PRIOR_RELEASE_ID.encode(), RELEASE_ID.encode())
        )
        target.chmod(0o444)
        with self.assertRaisesRegex(bundle.BundleValidationError, "digest/size"):
            self.fixture.verify(frozen.root)

    def test_rejects_extra_file(self):
        frozen = self.fixture.freeze()
        frozen.root.chmod(0o755)
        extra = frozen.root / "extra.json"
        extra.write_text("{}\n", encoding="utf-8")
        extra.chmod(0o444)
        frozen.root.chmod(0o555)
        with self.assertRaisesRegex(bundle.BundleValidationError, "closure"):
            self.fixture.verify(frozen.root)

    def test_rejects_symlink(self):
        frozen = self.fixture.freeze()
        target = frozen.root / "release-metrics.json"
        frozen.root.chmod(0o755)
        target.unlink()
        target.symlink_to(self.fixture.paths["release_metrics"])
        frozen.root.chmod(0o555)
        with self.assertRaisesRegex(bundle.BundleValidationError, "symlink"):
            self.fixture.verify(frozen.root)

    def test_rejects_hard_link(self):
        frozen = self.fixture.freeze()
        target = frozen.root / "release-metrics.json"
        outside = self.fixture.root / "outside.json"
        outside.write_bytes(target.read_bytes())
        outside.chmod(0o444)
        frozen.root.chmod(0o755)
        target.unlink()
        os.link(outside, target)
        frozen.root.chmod(0o555)
        with self.assertRaisesRegex(bundle.BundleValidationError, "hard links"):
            self.fixture.verify(frozen.root)

    def test_rejects_noncanonical_evidence_even_with_updated_identity(self):
        frozen = self.fixture.freeze()
        old_root = frozen.root
        old_root.chmod(0o755)
        target = old_root / "release-metrics.json"
        target.chmod(0o644)
        noncanonical = json.dumps(self.fixture.metrics, indent=2, sort_keys=True).encode("utf-8")
        target.write_bytes(noncanonical)
        target.chmod(0o444)

        manifest_path = old_root / bundle.MANIFEST_NAME
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest["files"]:
            if item["path"] == "release-metrics.json":
                item["bytes"] = len(noncanonical)
                item["sha256"] = hashlib.sha256(noncanonical).hexdigest()
        identity = dict(manifest)
        identity.pop("bundle_id")
        manifest["bundle_id"] = bundle._bundle_id(identity)
        manifest_path.write_bytes(bundle._canonical_json_bytes(manifest))
        manifest_path.chmod(0o444)
        new_root = old_root.with_name(manifest["bundle_id"].removeprefix("sha256:"))
        old_root.rename(new_root)
        new_root.chmod(0o555)
        with self.assertRaisesRegex(bundle.BundleValidationError, "not canonical JSON"):
            self.fixture.verify(new_root)


if __name__ == "__main__":
    unittest.main()
