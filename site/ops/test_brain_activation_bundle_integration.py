#!/usr/bin/env python3
"""One real-artifact integration test for the P1B activation bundle."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BRAIN = ROOT / "brain"
TOOLS = BRAIN / "tools"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(BRAIN))

import authority_contracts as contracts  # noqa: E402
import brain_promote_release as promoter  # noqa: E402
import brain_public_baseline as public_baseline  # noqa: E402
import build_release  # noqa: E402
import measure_store  # noqa: E402
import semantic_diff  # noqa: E402
import store as brain_store  # noqa: E402
import test_authority_contracts as authority_test_helpers  # noqa: E402


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {arguments!r}\n{result.stderr}"
        )
    return result


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _populate_public_source(root: Path) -> None:
    def write(relative: str, data: bytes | None = None) -> None:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if data is not None else f"fixture:{relative}\n".encode())

    for relative in sorted(public_baseline.CRITICAL_PATHS):
        if not relative.endswith("/manifest.json"):
            write(relative)
    write(
        "assets/decl-index/manifest.json",
        b'{"shards":{"aa":1}}',
    )
    write("assets/decl-index/aa.json", b'[["A.a","A"]]\n')
    write(
        "assets/suffix-index/manifest.json",
        b'{"shards":{"aa":1}}',
    )
    write("assets/suffix-index/aa.json", b'{"aa":[["A.a","A"]]}\n')
    write(
        "assets/premise-index/manifest.json",
        b'{"chunks":1,"shards":{"aa":1}}',
    )
    write("assets/premise-index/aa.json", b'{"A.a":[0]}\n')
    write("assets/premise-index/names/0.json", b'["A.a"]\n')
    write("assets/icons/wiki.svg", b"<svg/>\n")
    # Brain-owned files prove that the baseline freezer excludes this namespace.
    write("brain.html", b"not part of the public baseline\n")
    write("assets/brain/current.json", b"{}\n")


def _tree_inventory(root: Path) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(b"wikilean\0wikilean.file-tree.v1\0")
    objects = 0
    byte_count = 0
    for path in sorted(
        resolved.rglob("*"), key=lambda value: value.relative_to(resolved).as_posix()
    ):
        if path.is_dir():
            continue
        relative = path.relative_to(resolved).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(hashlib.sha256(body).digest())
        objects += 1
        byte_count += len(body)
    return {
        "schema": "wikilean.file-tree-inventory/v1",
        "root": str(resolved),
        "objects": objects,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


class BrainActivationBundleIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.git = self._required_tool("git")
        self.node = self._required_tool("node")
        self.npm = self._required_tool("npm")

    @staticmethod
    def _required_tool(name: str) -> Path:
        selected = shutil.which(name)
        if selected is None:
            raise RuntimeError(f"integration test requires {name}")
        return Path(selected).resolve(strict=True)

    def tearDown(self) -> None:
        # Baseline and activation stores deliberately publish 0555/0444 trees.
        for current, dirnames, filenames in os.walk(
            self.base, topdown=False, followlinks=False
        ):
            current_path = Path(current)
            for name in filenames:
                with contextlib.suppress(OSError):
                    (current_path / name).chmod(0o600)
            for name in dirnames:
                child = current_path / name
                if not child.is_symlink():
                    with contextlib.suppress(OSError):
                        child.chmod(0o700)
            with contextlib.suppress(OSError):
                current_path.chmod(0o700)
        self.temporary.cleanup()

    def _git(self, root: Path, *arguments: str) -> str:
        return _run(
            [str(self.git), "-C", str(root), *arguments], cwd=self.base
        ).stdout.strip()

    def _copy_runtime(self, promotion: Path) -> None:
        for relative in (
            "site/ops/brain_activation_bundle.py",
            "site/ops/brain_activation_ci.py",
            "site/ops/brain_deploy_journal.py",
            "site/ops/brain_http.py",
            "site/ops/brain_promote_release.py",
            "site/ops/brain_public_baseline.py",
            "brain/tools/authority_contracts.py",
            "brain/tools/execution_environment.py",
            "brain/tools/build_release.py",
            "brain/tools/measure_store.py",
            "brain/tools/semantic_diff.py",
        ):
            destination = promotion / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        (promotion / "wiki").mkdir(parents=True, exist_ok=True)
        _write_json(
            promotion / "wiki/package.json",
            {
                "name": "wikilean-activation-integration",
                "version": "1.0.0",
                "private": True,
                "scripts": {"test:ci": "node test-ci.js"},
            },
        )
        _write_json(
            promotion / "wiki/package-lock.json",
            {
                "name": "wikilean-activation-integration",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {
                        "name": "wikilean-activation-integration",
                        "version": "1.0.0",
                    }
                },
            },
        )
        (promotion / "wiki/test-ci.js").write_text(
            "if (!process.version.startsWith('v22.')) process.exit(1);\n"
            "console.log('worker integration gate passed');\n",
            encoding="utf-8",
        )
        (promotion / "scripts").mkdir(parents=True, exist_ok=True)
        ci_script = promotion / "scripts/ci-python.sh"
        ci_script.write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        ci_script.chmod(0o755)
        (promotion / ".gitignore").write_text(
            "__pycache__/\n*.pyc\nwiki/node_modules/\n",
            encoding="utf-8",
        )

    def _build_release(
        self,
        build_root: Path,
        release_store: Path,
        authority: str,
        version: str,
    ) -> dict[str, object]:
        return build_release.build_release(
            build_release.BuildConfig(
                repo_root=build_root,
                output_store=release_store,
                semantic_epoch="brain-v3-current",
                schedule="brain-v3-current",
                reducer_version=version,
                authority_git_commit=authority,
                reducer_git_commit=authority,
                configuration_sha256="2" * 64,
                environment_sha256="3" * 64,
            )
        )

    def test_real_release_and_public_baseline_freeze_and_verify(self) -> None:
        public_source = self.base / "public-source"
        public_source.mkdir()
        _populate_public_source(public_source)

        promotion = self.base / "promotion"
        promotion.mkdir()
        self._copy_runtime(promotion)
        attestation_path = promotion / public_baseline.SOURCE_ATTESTATION_PATH
        attestation_path.parent.mkdir(parents=True, exist_ok=True)
        attestation_path.write_bytes(
            public_baseline.render_source_attestation(public_source)
        )
        self._git(promotion, "init", "-q", "--initial-branch=main")
        self._git(promotion, "config", "user.name", "Activation Integration")
        self._git(
            promotion,
            "config",
            "user.email",
            "activation-integration@example.invalid",
        )
        self._git(promotion, "config", "commit.gpgsign", "false")
        self._git(promotion, "config", "core.hooksPath", "/dev/null")
        self._git(
            promotion,
            "add",
            ".gitignore",
            "brain",
            "scripts",
            "site",
            "wiki",
        )
        self._git(promotion, "commit", "-q", "-m", "activation authority")
        authority = self._git(promotion, "rev-parse", "HEAD")

        build_root = self.base / "build"
        self._git(
            promotion,
            "worktree",
            "add",
            "--detach",
            str(build_root),
            authority,
        )
        fixture = authority_test_helpers.ReleaseVerificationTest(methodName="runTest")
        fixture.temp = None
        fixture.root = build_root
        fixture.write_release_artifacts()
        edges_path = build_root / "brain/data/edges.jsonl"
        edge_lines = edges_path.read_text(encoding="utf-8").splitlines()
        edge = json.loads(edge_lines[1])
        edge["provenance"] = {"source": "wikidata"}
        edges_path.write_text(
            edge_lines[0]
            + "\n"
            + json.dumps(edge, separators=(",", ":"), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        brain_store.write_sqlite_from_jsonl(
            build_root / "brain/data/brain.sqlite3",
            build_root / "brain/data",
        )
        inventory = {
            "schema": "wikilean.reducer-input-inventory/v1",
            "scope": ["fixture"],
            "classes": {},
            "inputs": [
                {
                    "path": "catalog/data/source_registry.json",
                    "class": "curated_git_input",
                    "consumers": ["fixture"],
                    "purpose": "fixture",
                }
            ],
        }
        inventory_path = build_root / "brain/authority/reducer-inputs-v1.json"
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_bytes(contracts.canonical_json_bytes(inventory))

        release_store = self.base / "releases"
        prior = self._build_release(
            build_root, release_store, authority, "integration-prior"
        )
        candidate = self._build_release(
            build_root, release_store, authority, "integration-candidate"
        )
        self.assertNotEqual(prior["release_id"], candidate["release_id"])
        contracts.verify_release_files(
            contracts.validate_release_manifest(
                contracts.load_canonical_json(Path(candidate["manifest"]))[0]
            ),
            Path(candidate["root"]),
        )

        baseline = public_baseline.freeze_public_baseline(
            public_source,
            self.base / "public-baselines",
            authority,
            promotion,
        )
        verified_baseline = public_baseline.verify_public_baseline(
            baseline.root,
            promotion,
            expected_baseline_id=baseline.baseline_id,
            expected_authority_git_commit=authority,
            git_executable=self.git,
        )
        self.assertEqual(verified_baseline, baseline)

        python = str(Path(sys.executable).resolve())
        isolated_env = dict(os.environ)
        isolated_env["PYTHONDONTWRITEBYTECODE"] = "1"
        isolated_env["LC_ALL"] = "C"
        hostile_bin = self.base / "hostile-bin"
        hostile_bin.mkdir()
        for name in ("git", "node", "npm"):
            shadow = hostile_bin / name
            shadow.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
            shadow.chmod(0o755)
        isolated_env["PATH"] = str(hostile_bin)
        activation_script = promotion / "site/ops/brain_activation_bundle.py"
        evidence = self.base / "evidence"
        evidence.mkdir()
        context_result = _run(
            [
                python,
                str(activation_script),
                "context",
                "--build-worktree",
                str(build_root),
                "--promotion-worktree",
                str(promotion),
                "--git",
                str(self.git),
            ],
            cwd=promotion,
            env=isolated_env,
        )
        (evidence / "build-context.json").write_text(
            context_result.stdout, encoding="utf-8"
        )

        semantic_report = semantic_diff.compare_paths(
            Path(prior["manifest"]), Path(candidate["manifest"])
        )
        self.assertTrue(semantic_report["coverage"]["complete"])
        self.assertFalse(semantic_report["different"])
        _write_json(evidence / "semantic-diff.json", semantic_report)

        metrics = measure_store.measure_database(
            Path(candidate["root"]) / "brain/data/brain.sqlite3",
            release_id=str(candidate["release_id"]),
            release_id_source=str(candidate["manifest"]),
            limit=100,
            iterations=5,
            warmup=1,
            check_limit=100,
        )
        self.assertTrue(metrics["ok"])
        self.assertEqual(metrics["warnings"], [])
        _write_json(evidence / "release-metrics.json", metrics)
        _write_json(evidence / "release-result.json", candidate)

        public_dir = build_root / "wiki/public"
        stage_result = json.loads(
            _run(
                [
                    str(self.node),
                    "--experimental-strip-types",
                    str(ROOT / "wiki/scripts/brain-release-public.ts"),
                    "--manifest",
                    str(candidate["manifest"]),
                    "--release-dir",
                    str(candidate["root"]),
                    "--destination",
                    str(public_dir / "assets/brain"),
                    "--brain-page-destination",
                    str(public_dir / "brain.html"),
                    "--min-free-bytes",
                    "0",
                ],
                cwd=ROOT,
                env=isolated_env,
            ).stdout
        )
        shadow_public = {
            "schema": "wikilean.public-build-result/v1",
            "public_dir": str(public_dir),
            "mathlib_declarations": 1,
            "public_baseline": None,
            "brain": stage_result,
            "duration_ms": stage_result["duration_ms"],
            "max_rss_bytes": stage_result["max_rss_bytes"],
        }
        _write_json(evidence / "shadow-public-result.json", shadow_public)

        node_version = _run([str(self.node), "--version"], cwd=promotion).stdout

        baseline_files = len(baseline.files)
        baseline_bytes = baseline.total_bytes
        attempt_id = "20300101T000000Z-111111111111-deadbeef00"
        audited_at = "2030-01-01T00:00:00Z"
        promoter_work = self.base / "promoter-work"
        promoter_public_dir = promoter_work / "public"
        shutil.copytree(public_dir, promoter_public_dir)
        for item in baseline.files:
            destination = promoter_public_dir / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline.root / item.path, destination)
        worker_dir = promoter_work / "worker"
        worker_dir.mkdir(parents=True)
        worker_entry = worker_dir / "index.js"
        worker_entry.write_text("export default {};\n", encoding="utf-8")
        deploy_config = promoter_work / "wrangler.jsonc"
        deploy_config.write_text("{}\n", encoding="utf-8")
        public_inventory = promoter.inventory_tree(promoter_public_dir)
        worker_inventory = promoter.inventory_tree(worker_dir)
        promoter.seal_tree_read_only(promoter_public_dir)
        promoter.seal_tree_read_only(worker_dir)
        deploy_config.chmod(0o400)

        promoter_brain = json.loads(json.dumps(stage_result))
        promoter_brain["destination"] = str(promoter_public_dir / "assets/brain")
        promoter_brain["brain_page"]["destination"] = str(
            promoter_public_dir / "brain.html"
        )
        promoter_public_result = {
            **shadow_public,
            "public_dir": str(promoter_public_dir),
            "public_baseline": {
                "schema": "wikilean.public-asset-baseline/v1",
                "baseline_id": baseline.baseline_id,
                "authority_commit": authority,
                "root": str(baseline.root),
                "files": baseline_files,
                "bytes": baseline_bytes,
            },
            "brain": promoter_brain,
        }
        candidate_info = promoter.ReleaseInfo(
            str(candidate["release_id"]),
            str(candidate["release"]),
            Path(candidate["root"]),
            Path(candidate["manifest"]),
            hashlib.sha256(Path(candidate["manifest"]).read_bytes()).hexdigest(),
            authority,
            authority,
            promoter.inventory_tree(Path(candidate["root"])),
        )
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
        staged_body = (promoter_public_dir / "assets/brain/current.json").read_bytes()
        audited_at = str(json.loads(staged_body)["audited_at"])
        staged_selector = promoter.SelectorState(
            200,
            hashlib.sha256(staged_body).hexdigest(),
            staged_body,
            str(candidate["release_id"]),
            None,
            None,
            audited_at,
        )
        prepared = promoter.PreparedPromotion(
            attempt_id,
            audited_at,
            f"brain-{str(candidate['release'])[:12]}-{attempt_id[-10:]}",
            f"Brain release {candidate['release_id']} attempt {attempt_id}",
            candidate_info,
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
            promoter_public_dir,
            public_inventory,
            promoter_public_result,
            staged_selector,
            worker_dir,
            worker_entry,
            worker_inventory,
            deploy_config,
            hashlib.sha256(deploy_config.read_bytes()).hexdigest(),
            node_version.strip(),
            "4.120.0",
            "certifi:integration",
            history,
            history_raw,
        )
        receipt_root = self.base / "receipts"
        receipt_root.mkdir()
        retained = promoter.retain_dry_run_artifacts(
            prepared,
            self.base / "retained-dry-runs",
            repo_root=promotion,
            receipt_root=receipt_root,
        )
        retained_brain = json.loads(json.dumps(stage_result))
        retained_brain["destination"] = str(retained.public_dir / "assets/brain")
        retained_brain["brain_page"]["destination"] = str(
            retained.public_dir / "brain.html"
        )
        retained_public_result = {
            **shadow_public,
            "public_dir": str(retained.public_dir),
            "public_baseline": promoter_public_result["public_baseline"],
            "brain": retained_brain,
        }
        dry_run = {
            "schema": "wikilean.brain-promotion-dry-run/v1",
            "ok": True,
            "attempt_id": attempt_id,
            "proposed_intent": {
                "requested_release_id": candidate["release_id"],
                "release_root": candidate["root"],
                "release_manifest_sha256": hashlib.sha256(
                    Path(candidate["manifest"]).read_bytes()
                ).hexdigest(),
                "release_tree": _tree_inventory(Path(candidate["root"])),
                "authority_commit": authority,
                "reducer_commit": authority,
                "retained_release": None,
                "public_baseline": {
                    "baseline_id": baseline.baseline_id,
                    "root": str(baseline.root),
                    "manifest": str(baseline.manifest_path),
                    "manifest_sha256": hashlib.sha256(
                        baseline.manifest_path.read_bytes()
                    ).hexdigest(),
                    "authority_commit": authority,
                    "files": baseline_files,
                    "bytes": baseline_bytes,
                },
                "public_tree": promoter.inventory_tree(retained.public_dir),
                "public_result": retained_public_result,
                "staged_selector": {
                    "sha256": hashlib.sha256(staged_body).hexdigest(),
                    "release_id": candidate["release_id"],
                    "previous_release_id": None,
                    "audited_at": audited_at,
                },
                "worker_bundle": {
                    "tree": promoter.inventory_tree(retained.worker_dir),
                    "entry": str(retained.worker_entry),
                    "config": str(retained.config),
                    "config_sha256": hashlib.sha256(
                        retained.config.read_bytes()
                    ).hexdigest(),
                    "node_version": node_version.strip(),
                    "wrangler_version": "4.120.0",
                },
                "audited_at": audited_at,
                "base_url": "https://wikilean.jackmccarthy.org",
                "trust_source": "certifi:integration",
                "predeploy": {
                    "deployment_id": "11111111-1111-1111-1111-111111111111",
                    "version_id": "22222222-2222-2222-2222-222222222222",
                    "status_sha256": predeploy.raw_sha256,
                    "selector_status": 404,
                    "selector_sha256": hashlib.sha256(b"").hexdigest(),
                    "release_id": None,
                    "previous_release_id": None,
                    "audited_at": None,
                },
                "planned": {
                    "tag": (
                        f"brain-{str(candidate['release_id']).removeprefix('sha256:')[:12]}-"
                        f"{attempt_id[-10:]}"
                    ),
                    "message": (
                        f"Brain release {candidate['release_id']} attempt {attempt_id}"
                    ),
                    "command_timeout_seconds": "900",
                },
                "approval_note": None,
                "first_deploy_exception": True,
                "first_deploy_approval": "integration fixture approval",
                "history": {
                    "deployments": history["deployments"],
                    "versions": history["versions"],
                },
                "retained_artifacts": retained.reference(),
            },
            "production_mutated": False,
        }
        _write_json(evidence / "promoter-dry-run.json", dry_run)

        freeze = _run(
            [
                python,
                str(activation_script),
                "freeze",
                "--release-manifest",
                str(candidate["manifest"]),
                "--semantic-baseline-manifest",
                str(prior["manifest"]),
                "--expected-semantic-baseline-id",
                str(prior["release_id"]),
                "--public-baseline-manifest",
                str(baseline.manifest_path),
                "--source-attestation",
                str(attestation_path),
                "--release-result",
                str(evidence / "release-result.json"),
                "--release-metrics",
                str(evidence / "release-metrics.json"),
                "--shadow-public-result",
                str(evidence / "shadow-public-result.json"),
                "--semantic-diff",
                str(evidence / "semantic-diff.json"),
                "--promoter-dry-run",
                str(evidence / "promoter-dry-run.json"),
                "--build-context",
                str(evidence / "build-context.json"),
                "--git",
                str(self.git),
                "--node",
                str(self.node),
                "--npm",
                str(self.npm),
                "--python",
                python,
                "--output-store",
                str(self.base / "activation-bundles"),
            ],
            cwd=promotion,
            env=isolated_env,
        )
        result = json.loads(freeze.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["release_id"], candidate["release_id"])
        self.assertEqual(result["semantic_baseline_release_id"], prior["release_id"])
        self.assertEqual(result["baseline_id"], baseline.baseline_id)

        verify = _run(
            [
                python,
                str(activation_script),
                "verify",
                "--bundle-root",
                result["root"],
                "--expected-bundle-id",
                result["bundle_id"],
                "--expected-semantic-baseline-id",
                str(prior["release_id"]),
            ],
            cwd=promotion,
            env=isolated_env,
        )
        verified = json.loads(verify.stdout)
        self.assertEqual(verified["bundle_id"], result["bundle_id"])


if __name__ == "__main__":
    unittest.main()
