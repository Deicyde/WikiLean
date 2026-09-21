#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location(
    "brain_promote_release", HERE / "brain_promote_release.py"
)
assert SPEC and SPEC.loader
promote = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promote
SPEC.loader.exec_module(promote)

from brain_deploy_journal import (
    EventJournal,
    PromotionLock,
    initialize_target_receipt_root,
)
from brain_http import SelectorProbe


RELEASE_A = "sha256:" + "a" * 64
RELEASE_B = "sha256:" + "b" * 64
RELEASE_C = "sha256:" + "e" * 64
BASELINE_ID = "sha256:" + "d" * 64
COMMIT = "c" * 40
PRIOR_COMMIT = "f" * 40
DEPLOYMENT_A = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_B = "22222222-2222-2222-2222-222222222222"
VERSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
VERSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DEPLOYMENT_C = "33333333-3333-3333-3333-333333333333"
VERSION_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def node_modules_identity(base: Path) -> dict[str, object]:
    return {
        "schema": "wikilean.node-modules-inventory/v1",
        "root": str((base / "node_modules").resolve()),
        "package_lock_sha256": "1" * 64,
        "objects": 1,
        "bytes": 1,
        "symlinks": 0,
        "sha256": "2" * 64,
    }


def node_executables_identity(base: Path) -> dict[str, object]:
    return {
        "schema": "wikilean.node-executables/v1",
        "node": {
            "path": str((base / "bin" / "node").resolve()),
            "sha256": "3" * 64,
            "bytes": 1,
        },
        "wrangler": {
            "path": str((base / "node_modules" / "wrangler.js").resolve()),
            "sha256": "4" * 64,
            "bytes": 1,
        },
    }


def make_repo(root: Path) -> Path:
    repo = (root / "repo").resolve()
    for relative in (
        "brain/tools/verify_release.py",
        "wiki/scripts/build-public.ts",
        "wiki/wrangler.jsonc",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if relative.endswith("wrangler.jsonc") else "# fixture\n", encoding="utf-8")
    (repo / ".git-fixture").mkdir()
    return repo


def make_release(base: Path, release_id: str = RELEASE_A) -> promote.ReleaseInfo:
    release_hex = release_id.removeprefix("sha256:")
    manifest_body = (
        json.dumps(
            {
                "schema": "wikilean.release/v1",
                "release_id": release_id,
                "authority": {"git_commit": COMMIT},
                "reducer": {"git_commit": COMMIT},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
    root = (base / "store" / manifest_sha256).resolve()
    (root / "site" / "out").mkdir(parents=True)
    (root / "site" / "out" / "brain.html").write_text("<html>brain</html>\n", encoding="utf-8")
    manifest = root / "release.json"
    manifest.write_bytes(manifest_body)
    return promote.ReleaseInfo(
        release_id,
        release_hex,
        root,
        manifest,
        manifest_sha256,
        COMMIT,
        COMMIT,
        promote.inventory_tree(root),
    )


def make_baseline(base: Path) -> promote.PublicAssetBaseline:
    baseline_hex = BASELINE_ID.removeprefix("sha256:")
    root = (base / "baseline" / baseline_hex).resolve()
    root.mkdir(parents=True)
    manifest = root / "manifest.json"
    manifest.write_text('{"fixture":true}\n', encoding="utf-8")
    return promote.PublicAssetBaseline(
        root,
        manifest,
        BASELINE_ID,
        baseline_hex,
        COMMIT,
        (),
        0,
    )


def selector_probe(release_id: str = RELEASE_A, *, status: int = 200, body: bytes | None = None):
    release_hex = release_id.removeprefix("sha256:")
    if body is None:
        body = (
            json.dumps(
                {
                    "schema": "wikilean.release-selector/v1",
                    "release_id": release_id,
                    "release": release_hex,
                    "manifest": f"/assets/brain/releases/{release_hex}/release.json",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
    return SelectorProbe(
        body=body,
        body_sha256=hashlib.sha256(body).hexdigest(),
        content_type="application/json",
        status=status,
        trust_source="fixture-ca",
        url="https://example.test/assets/brain/current.json?x=1",
    )


def make_prepared(base: Path) -> promote.PreparedPromotion:
    candidate = make_release(base, RELEASE_A)
    prior = make_release(base, RELEASE_B)
    baseline = make_baseline(base)
    public_dir = (base / "public").resolve()
    public_dir.mkdir()
    (public_dir / "brain.html").write_text("<html>brain</html>\n", encoding="utf-8")
    bundle_dir = (base / "bundle").resolve()
    bundle_dir.mkdir()
    bundle_entry = bundle_dir / "index.js"
    bundle_entry.write_text("export default {};\n", encoding="utf-8")
    deploy_config = (base / "wrangler.jsonc").resolve()
    deploy_config.write_text("{}\n", encoding="utf-8")
    initial_probe = selector_probe(RELEASE_B)
    initial_selector = promote.selector_from_probe(
        initial_probe,
        RELEASE_A,
        candidate_manifest_sha256=candidate.manifest_sha256,
        allow_first_deploy=False,
    )
    status_body = (
        json.dumps(
            {
                "id": DEPLOYMENT_A,
                "versions": [{"version_id": VERSION_A, "percentage": 100}],
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    staged_body = selector_probe(
        RELEASE_A,
        body=(
            json.dumps(
                {
                    "schema": "wikilean.release-selector/v2",
                    "release_id": RELEASE_A,
                    "release": "a" * 64,
                    "manifest_sha256": candidate.manifest_sha256,
                    "manifest": (
                        f"/assets/brain/releases/{candidate.manifest_sha256}/release.json"
                    ),
                    "previous_release_id": RELEASE_B,
                    "previous_release": "b" * 64,
                    "previous_manifest_sha256": prior.manifest_sha256,
                    "previous_manifest": (
                        f"/assets/brain/releases/{prior.manifest_sha256}/release.json"
                    ),
                    "audited_at": "2030-01-01T00:00:00Z",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    return promote.PreparedPromotion(
        "attempt-fixture",
        "2030-01-01T00:00:00Z",
        "brain-fixture",
        "Brain fixture attempt",
        candidate,
        prior,
        prior,
        baseline,
        initial_selector,
        promote.DeploymentState(
            DEPLOYMENT_A,
            VERSION_A,
            hashlib.sha256(status_body).hexdigest(),
        ),
        status_body,
        status_body,
        public_dir,
        promote.inventory_tree(public_dir),
        {"schema": "wikilean.public-build-result/v1", "duration_ms": 1.25},
        promote.selector_from_probe(
            staged_body,
            RELEASE_A,
            candidate_manifest_sha256=candidate.manifest_sha256,
            allow_first_deploy=False,
        ),
        bundle_dir,
        bundle_entry,
        promote.inventory_tree(bundle_dir),
        deploy_config,
        hashlib.sha256(deploy_config.read_bytes()).hexdigest(),
        "v22.23.2",
        "4.120.0",
        "fixture-ca",
        {},
        {"deployments": b'[{"id":"deployment"}]\n', "versions": b'[{"id":"version"}]\n'},
        wrangler_installation=node_modules_identity(base),
        node_executables=node_executables_identity(base),
    )


def with_reviewed_activation(
    prepared: promote.PreparedPromotion,
) -> promote.PreparedPromotion:
    root = prepared.public_dir.parent.resolve()
    retained = promote.RetainedDryRunArtifacts(
        "sha256:" + "9" * 64,
        "9" * 64,
        root,
        root / "manifest.json",
        "8" * 64,
        prepared.public_dir,
        prepared.bundle_dir,
        prepared.bundle_entry,
        prepared.deploy_config,
    )
    activation = promote.ReviewedActivation(
        root,
        root / "activation-manifest.json",
        "7" * 64,
        "sha256:" + "6" * 64,
        RELEASE_C,
        prepared.candidate.release_id,
        prepared.public_baseline.baseline_id,
        prepared.candidate.authority_commit,
        prepared.candidate.reducer_commit,
        prepared.attempt_id,
        {},
        retained,
    )
    return replace(prepared, activation=activation)


def prepared_for_retention(base: Path) -> promote.PreparedPromotion:
    prepared = make_prepared(base)
    selector_body = prepared.initial_selector.body
    status_before = prepared.predeploy_status_before
    histories = prepared.history_raw or {}
    history = {
        key: {
            "sha256": hashlib.sha256(histories[key]).hexdigest(),
            "bytes": len(histories[key]),
            "entries": 1,
        }
        for key in ("deployments", "versions")
    }
    page = prepared.public_dir / "brain.html"
    public_result = {
        "schema": "wikilean.public-build-result/v1",
        "public_dir": str(prepared.public_dir),
        "mathlib_declarations": 1,
        "public_baseline": {
            "schema": "wikilean.public-asset-baseline/v1",
            "baseline_id": prepared.public_baseline.baseline_id,
            "authority_commit": prepared.public_baseline.authority_git_commit,
            "root": str(prepared.public_baseline.root),
            "files": 0,
            "bytes": 0,
        },
        "brain": {
            "schema": "wikilean.public-stage-result/v2",
            "release_id": prepared.candidate.release_id,
            "release": prepared.candidate.release_hex,
            "manifest_sha256": prepared.candidate.manifest_sha256,
            "previous_release_id": prepared.prior.release_id if prepared.prior else None,
            "previous_manifest_sha256": (
                prepared.prior.manifest_sha256 if prepared.prior else None
            ),
            "retained_release_ids": [
                prepared.candidate.release_id,
                *([prepared.prior.release_id] if prepared.prior else []),
            ],
            "retained_manifest_sha256s": [
                prepared.candidate.manifest_sha256,
                *([prepared.prior.manifest_sha256] if prepared.prior else []),
            ],
            "destination": str(prepared.public_dir / "assets/brain"),
            "objects": 1,
            "bytes": page.stat().st_size,
            "largest_file_bytes": page.stat().st_size,
            "copy_buffer_bytes": 1024,
            "duration_ms": 1.0,
            "max_rss_bytes": 1024,
            "free_bytes_before": 100,
            "free_bytes_after": 99,
            "brain_page": {
                "destination": str(page),
                "bytes": page.stat().st_size,
                "sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
            },
            "warnings": [],
        },
        "duration_ms": 1.25,
        "max_rss_bytes": 2048,
    }
    return replace(
        prepared,
        initial_selector=replace(
            prepared.initial_selector,
            body_sha256=hashlib.sha256(selector_body).hexdigest(),
        ),
        predeploy=replace(
            prepared.predeploy,
            raw_sha256=hashlib.sha256(status_before).hexdigest(),
        ),
        public_result=public_result,
        history=history,
    )


def make_observation(
    selector: promote.SelectorState | None,
    *,
    deployment: promote.DeploymentState | None = None,
    annotations: dict[str, str] | None = None,
    trust_source: str = "fixture-ca",
    selector_sha256: str | None = None,
) -> promote.ReconciliationObservation:
    if selector is None:
        body = b"missing"
        status = 404
        digest = selector_sha256 or hashlib.sha256(body).hexdigest()
    else:
        body = selector.body
        status = selector.status
        digest = selector_sha256 or selector.body_sha256
    probe = SelectorProbe(
        body=body,
        body_sha256=digest,
        content_type="application/json" if status == 200 else "application/octet-stream",
        status=status,
        trust_source=trust_source,
        url="https://example.test/assets/brain/current.json?reconcile=1",
    )
    return promote.ReconciliationObservation(
        deployment or promote.DeploymentState(DEPLOYMENT_B, VERSION_B, "7" * 64),
        probe,
        selector,
        annotations or {},
        trust_source,
        b'{"status":"before"}',
        b'{"status":"after"}',
    )


class ResultRunner(promote.CommandRunner):
    def __init__(self, result: promote.RunResult | BaseException):
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, cwd, timeout=None, env=None):
        del cwd, timeout, env
        self.calls.append(tuple(args))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class DeployScenario(promote.BrainPromoter):
    def __init__(self, repo: Path, receipt: Path, runner: ResultRunner, **kwargs):
        super().__init__(
            repo_root=repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=receipt.parent / "unused" / ("a" * 64),
            public_baseline_id=BASELINE_ID,
            public_baseline_root=receipt.parent / "unused-baseline" / ("d" * 64),
            receipt_root=receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="execute",
            approval_note="fixture approval",
            runner=runner,
            attempt_id="attempt-fixture",
            audited_at="2030-01-01T00:00:00Z",
            status_attempts=1,
            status_interval=0,
            **kwargs,
        )
        self.candidate_state = promote.DeploymentState(DEPLOYMENT_B, VERSION_B, "3" * 64)
        self.canary_ok = True
        self.observe_error: Exception | None = None

    def _wait_for_attempt_version(self, **_kwargs):
        return self.candidate_state, {"workers/tag": "brain-fixture"}, []

    def _run_canary(
        self,
        release_id,
        manifest_sha256,
        journal,
        prefix,
        expected_trust_source,
        public_baseline=None,
    ):
        del journal, prefix, public_baseline
        result = promote.RunResult(("canary",), 0 if self.canary_ok else 1, b"", b"")
        return self.canary_ok, {
            "expected_release_id": release_id,
            "expected_manifest_sha256": manifest_sha256,
            "expected_trust_source": expected_trust_source,
            "result": {
                "ok": self.canary_ok,
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "trust_source": expected_trust_source,
            },
        }, result

    def _final_predeploy_fence(self, prepared, journal):
        del prepared, journal
        return {}

    def _remote_predeploy_fence(self, prepared, *, config, phase):
        del config, phase
        result = promote.RunResult(("status",), 0, b"{}", b"")
        probe = SelectorProbe(
            body=prepared.initial_selector.body,
            body_sha256=prepared.initial_selector.body_sha256,
            content_type="application/json",
            status=prepared.initial_selector.status,
            trust_source=prepared.trust_source,
            url="https://example.test/assets/brain/current.json?fence=2",
        )
        return prepared.predeploy, result, probe, prepared.predeploy, result

    def _wrangler_installation_identity(self):
        return node_modules_identity(self.repo.parent)

    def _node_executables_identity(self):
        return node_executables_identity(self.repo.parent)

    def _observe_expected_release(self, **_kwargs):
        if self.observe_error is not None:
            raise self.observe_error
        return {
            "deployment_id": DEPLOYMENT_B,
            "version_id": VERSION_B,
            "release_id": RELEASE_A,
        }


class FenceScenario(DeployScenario):
    def __init__(
        self,
        repo: Path,
        receipt: Path,
        runner: ResultRunner,
        prepared: promote.PreparedPromotion,
        *,
        probes: list[tuple[SelectorProbe, str]] | None = None,
        statuses: list[promote.DeploymentState] | None = None,
    ) -> None:
        super().__init__(repo, receipt, runner)
        self.prepared_value = prepared
        self.probes = list(
            probes
            or [
                (
                    SelectorProbe(
                        body=prepared.initial_selector.body,
                        body_sha256=prepared.initial_selector.body_sha256,
                        content_type="application/json",
                        status=prepared.initial_selector.status,
                        trust_source=prepared.trust_source,
                        url="https://example.test/assets/brain/current.json?fence=1",
                    ),
                    prepared.trust_source,
                )
            ]
        )
        self.statuses = list(statuses or [prepared.predeploy, prepared.predeploy])

    def _final_predeploy_fence(self, prepared, journal):
        return promote.BrainPromoter._final_predeploy_fence(self, prepared, journal)

    def _verify_reviewed_activation(self, expected=None):
        if expected is None:
            raise AssertionError("expected reviewed activation")
        return expected

    def _wrangler_installation_identity(self):
        return self.prepared_value.wrangler_installation

    def _node_executables_identity(self):
        return self.prepared_value.node_executables

    def _remote_predeploy_fence(self, prepared, *, config, phase):
        return promote.BrainPromoter._remote_predeploy_fence(
            self, prepared, config=config, phase=phase
        )

    def _verify_release(
        self, expected_release_id, root_input, *, allow_legacy_release_id_root=False
    ):
        del root_input, allow_legacy_release_id_root
        if expected_release_id == self.prepared_value.candidate.release_id:
            return self.prepared_value.candidate
        if (
            self.prepared_value.prior is not None
            and expected_release_id == self.prepared_value.prior.release_id
        ):
            return self.prepared_value.prior
        raise AssertionError(expected_release_id)

    def _check_git_authority(self, expected_commit):
        return expected_commit

    def _check_recovery_checkout(self, authority_commit):
        return authority_commit

    def _verify_toolchain(self):
        return self.prepared_value.node_version, self.prepared_value.wrangler_version

    def _probe_selector(self, phase):
        self.asserted_phase = phase
        return self.probes.pop(0)

    def _wrangler_status(self, config=None):
        del config
        state = self.statuses.pop(0)
        body = json.dumps(
            {"id": state.deployment_id, "version_id": state.version_id}, sort_keys=True
        ).encode()
        return state, promote.RunResult(("wrangler", "deployments", "status"), 0, body, b"")


class ReconcileScenario(promote.BrainPromoter):
    def __init__(
        self,
        repo: Path,
        receipt: Path,
        prepared: promote.PreparedPromotion,
        observations: list[promote.ReconciliationObservation],
        *,
        reconcile_attempt: str,
        reconcile_quiet_seconds: float = 1,
        confirm_no_production_change: bool = False,
        no_change_approval: str | None = None,
        deploy_invocation_started: bool = False,
        command_timeout: float = 1,
        allow_first_deploy: bool = False,
        first_deploy_approval: str | None = None,
        accept_external_supersession: bool = False,
        external_supersession_approval: str | None = None,
        attempt_histories: list[dict[str, object]] | None = None,
        local_process_snapshots: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.prepared_value = prepared
        self.observations = list(observations)
        self.observation_calls = 0
        self.canary_releases: list[tuple[str, str, str]] = []
        self.sleeps: list[float] = []
        self.canary_ok = True
        self.deploy_invocation_started = deploy_invocation_started
        self.attempt_histories = list(attempt_histories or [])
        self.local_process_snapshots = list(local_process_snapshots or [])
        super().__init__(
            repo_root=repo,
            python=Path(sys.executable),
            release_id=None,
            release_root=None,
            public_baseline_id=None,
            public_baseline_root=None,
            receipt_root=receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="reconcile",
            approval_note="fixture reconciliation approval",
            reconcile_attempt=reconcile_attempt,
            reconcile_quiet_seconds=reconcile_quiet_seconds,
            confirm_no_production_change=confirm_no_production_change,
            no_change_approval=no_change_approval,
            allow_first_deploy=allow_first_deploy,
            first_deploy_approval=first_deploy_approval,
            accept_external_supersession=accept_external_supersession,
            external_supersession_approval=external_supersession_approval,
            command_timeout=command_timeout,
            sleeper=self.sleeps.append,
            attempt_id="reconcile-command",
        )

    def _verify_release(
        self, expected_release_id, root_input, *, allow_legacy_release_id_root=False
    ):
        del root_input, allow_legacy_release_id_root
        if expected_release_id == self.prepared_value.candidate.release_id:
            return self.prepared_value.candidate
        if (
            self.prepared_value.prior is not None
            and expected_release_id == self.prepared_value.prior.release_id
        ):
            return self.prepared_value.prior
        raise AssertionError(expected_release_id)

    def _check_git_authority(self, expected_commit):
        return expected_commit

    def _check_recovery_checkout(self, authority_commit):
        return authority_commit

    def _verify_toolchain(self):
        return self.prepared_value.node_version, self.prepared_value.wrangler_version

    def _wrangler_installation_identity(self):
        return self.prepared_value.wrangler_installation

    def _node_executables_identity(self):
        return self.prepared_value.node_executables

    def _stable_reconciliation_observation(self, config=None):
        del config
        self.observation_calls += 1
        if not self.observations:
            raise AssertionError("unexpected reconciliation observation")
        return self.observations.pop(0)

    def _attempt_history(self, tag, message, config=None):
        del tag, message, config
        if self.attempt_histories:
            return self.attempt_histories.pop(0)
        return {
            "version_ids": [],
            "deployments": [],
            "versions_returned": 1,
            "deployments_returned": 1,
            "versions_sha256": "1" * 64,
            "deployments_sha256": "2" * 64,
        }

    def _local_attempt_processes(self, tag, message):
        del tag, message
        if self.local_process_snapshots:
            return self.local_process_snapshots.pop(0)
        return []

    def _run_canary(
        self,
        release_id,
        manifest_sha256,
        journal,
        prefix,
        expected_trust_source,
        public_baseline=None,
    ):
        del journal, public_baseline
        self.canary_releases.append((release_id, prefix, expected_trust_source))
        result = promote.RunResult(("canary",), 0 if self.canary_ok else 1, b"", b"")
        return self.canary_ok, {
            "expected_release_id": release_id,
            "expected_manifest_sha256": manifest_sha256,
            "expected_trust_source": expected_trust_source,
            "result": {
                "ok": self.canary_ok,
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "trust_source": expected_trust_source,
            },
        }, result


class InjectedCrash(BaseException):
    pass


class BrainPromotionUnitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.repo = make_repo(self.base)
        self.receipt = initialize_target_receipt_root(
            self.base / "receipts", self.repo, "https://example.test"
        )

    def tearDown(self):
        self.temp.cleanup()

    def promoter(self, **kwargs):
        mode = kwargs.pop("mode", "dry-run")
        return promote.BrainPromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=self.base / "store" / ("a" * 64),
            public_baseline_id=BASELINE_ID,
            public_baseline_root=self.base / "baseline" / ("d" * 64),
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode=mode,
            attempt_id="attempt-fixture",
            audited_at="2030-01-01T00:00:00Z",
            **kwargs,
        )

    def test_generated_attempt_id_is_accepted_by_the_promoter_contract(self):
        attempt_id = promote.BrainPromoter._new_attempt_id(RELEASE_A)
        self.assertIsNotNone(promote.ATTEMPT_ID_RE.fullmatch(attempt_id))

    def test_command_timeout_kills_the_entire_child_process_group(self):
        sentinel = self.base / "orphan-child-ran"
        child = (
            "import pathlib,sys,time; "
            "time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('late')"
        )
        parent = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
            "time.sleep(10)"
        )
        result = promote.CommandRunner().run(
            [sys.executable, "-c", parent, child, str(sentinel)],
            cwd=self.base,
            timeout=0.1,
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        time.sleep(0.5)
        self.assertFalse(sentinel.exists())

    def test_command_interrupt_kills_and_reaps_the_child_process_group(self):
        process = mock.Mock(pid=43210)
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
        with mock.patch.object(promote.subprocess, "Popen", return_value=process):
            with mock.patch.object(promote.os, "killpg") as killpg:
                with self.assertRaises(KeyboardInterrupt):
                    promote.CommandRunner().run(
                        ["wrangler", "deploy"], cwd=self.base, timeout=30
                    )
        killpg.assert_called_once_with(43210, promote.signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    def test_wrangler_installation_identity_detects_tampering_and_symlink_escape(self):
        wiki = self.base / "toolchain-wiki"
        package = wiki / "node_modules" / "wrangler"
        package.mkdir(parents=True)
        (wiki / "package-lock.json").write_text('{"lockfileVersion":3}\n')
        entry = package / "bin.js"
        entry.write_text("first\n")
        bin_dir = wiki / "node_modules" / ".bin"
        bin_dir.mkdir()
        (bin_dir / "wrangler").symlink_to("../wrangler/bin.js")
        before = promote.inventory_node_modules(wiki)
        entry.write_text("other\n")
        after = promote.inventory_node_modules(wiki)
        self.assertNotEqual(before["sha256"], after["sha256"])

        outside = self.base / "outside-node-tool"
        outside.write_text("outside\n")
        escaping = bin_dir / "escaping"
        escaping.symlink_to("../../../outside-node-tool")
        with self.assertRaisesRegex(promote.PromotionError, "escapes"):
            promote.inventory_node_modules(wiki)

    def test_node_environment_drops_injection_and_scopes_cloudflare_credentials(self):
        source = {
            "HOME": str(self.base),
            "NODE_OPTIONS": "--require=/tmp/inject.js",
            "NODE_PATH": "/tmp/inject-modules",
            "npm_config_userconfig": "/tmp/attacker.npmrc",
            "CLOUDFLARE_API_TOKEN": "token",
            "CLOUDFLARE_ACCOUNT_ID": "account",
            "CLOUDFLARE_API_BASE_URL": "https://attacker.invalid",
            "SSL_CERT_FILE": "/tmp/ca.pem",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            local = promote._node_environment(Path("/reviewed/bin/node"))
            remote = promote._node_environment(
                Path("/reviewed/bin/node"), include_cloudflare=True
            )
        self.assertNotIn("NODE_OPTIONS", local)
        self.assertNotIn("NODE_PATH", local)
        self.assertNotIn("npm_config_userconfig", local)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", local)
        self.assertEqual(remote["CLOUDFLARE_API_TOKEN"], "token")
        self.assertEqual(remote["CLOUDFLARE_ACCOUNT_ID"], "account")
        self.assertNotIn("CLOUDFLARE_API_BASE_URL", remote)
        self.assertEqual(remote["SSL_CERT_FILE"], "/tmp/ca.pem")

    def test_nonfinite_operational_intervals_are_rejected(self):
        for options in (
            {"command_timeout": float("inf")},
            {"reconcile_quiet_seconds": float("nan")},
            {"status_interval": float("inf")},
            {"canary_timeout": float("nan")},
        ):
            with self.subTest(options=options):
                with self.assertRaises(promote.PromotionError):
                    self.promoter(**options)._validate_options()

    def test_first_deploy_approval_is_execute_only(self):
        self.promoter(allow_first_deploy=True)._validate_options()
        for allow_first_deploy, approval in (
            (True, "premature approval"),
            (False, "orphan approval"),
            (True, ""),
        ):
            with self.subTest(
                allow_first_deploy=allow_first_deploy,
                approval=approval,
            ), self.assertRaisesRegex(
                promote.PromotionError, "valid only with --execute"
            ):
                self.promoter(
                    allow_first_deploy=allow_first_deploy,
                    first_deploy_approval=approval,
                )._validate_options()

    def test_approval_note_is_execute_only(self):
        for approval_note in ("premature approval", "", "   "):
            with self.subTest(approval_note=approval_note), self.assertRaisesRegex(
                promote.PromotionError, "valid only with --execute"
            ):
                self.promoter(approval_note=approval_note)._validate_options()

    def test_execute_and_reconciliation_require_nonempty_approval_notes(self):
        for approval_note in (None, "", "   "):
            with self.subTest(
                mode="execute", approval_note=approval_note
            ), self.assertRaisesRegex(
                promote.PromotionError, "execution requires --approval-note"
            ):
                self.promoter(
                    mode="execute",
                    approval_note=approval_note,
                )._validate_options()
            with self.subTest(
                mode="reconcile", approval_note=approval_note
            ), self.assertRaisesRegex(
                promote.PromotionError, "reconciliation requires --approval-note"
            ):
                self.promoter(
                    mode="reconcile",
                    reconcile_attempt="reconcile-approval-test",
                    approval_note=approval_note,
                )._validate_options()

    def test_execute_first_deploy_requires_nonempty_paired_approval(self):
        execute_options = {
            "mode": "execute",
            "activation_bundle_id": "sha256:" + "6" * 64,
            "activation_bundle_root": self.base / "activation-bundle",
            "expected_semantic_baseline_id": RELEASE_C,
            "approval_note": "approved reviewed activation",
        }
        with self.assertRaisesRegex(promote.PromotionError, "first-deploy-approval"):
            self.promoter(
                **execute_options,
                allow_first_deploy=True,
                first_deploy_approval="   ",
            )._validate_options()
        with self.assertRaisesRegex(promote.PromotionError, "requires --allow"):
            self.promoter(
                **execute_options,
                first_deploy_approval="orphan approval",
            )._validate_options()
        self.promoter(
            **execute_options,
            allow_first_deploy=True,
            first_deploy_approval="Jack approved this first deployment",
        )._validate_options()

    def test_reconciliation_approval_flags_require_nonempty_paired_text(self):
        prepared = make_prepared(self.base)
        for options, message in (
            (
                {
                    "confirm_no_production_change": True,
                    "no_change_approval": "",
                },
                "non-empty",
            ),
            (
                {"no_change_approval": "orphan approval"},
                "requires --confirm",
            ),
            (
                {"external_supersession_approval": "orphan approval"},
                "requires --accept",
            ),
            (
                {
                    "allow_first_deploy": True,
                    "first_deploy_approval": "orphan approval",
                },
                "not valid during reconciliation",
            ),
            (
                {
                    "confirm_no_production_change": True,
                    "no_change_approval": "approved no change",
                    "accept_external_supersession": True,
                    "external_supersession_approval": "approved external state",
                },
                "choose only one",
            ),
        ):
            scenario = ReconcileScenario(
                self.repo,
                self.receipt,
                prepared,
                [],
                reconcile_attempt="approval-pairing",
                **options,
            )
            with self.subTest(options=options):
                with self.assertRaisesRegex(promote.PromotionError, message):
                    scenario._validate_options()

    def test_execute_requires_complete_activation_trust_anchors(self):
        runner = ResultRunner(promote.RunResult(("unexpected",), 0, b"", b""))
        instance = promote.BrainPromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=self.base / "store" / ("a" * 64),
            public_baseline_id=BASELINE_ID,
            public_baseline_root=self.base / "baseline" / ("d" * 64),
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="execute",
            approval_note="fixture approval",
            runner=runner,
        )
        with self.assertRaisesRegex(promote.PromotionError, "activation-bundle-id"):
            instance.run()
        self.assertEqual(runner.calls, [])

    def test_first_deploy_execute_without_approval_blocks_before_any_runner_call(self):
        runner = ResultRunner(promote.RunResult(("unexpected",), 0, b"", b""))
        instance = self.promoter(
            mode="execute",
            activation_bundle_id="sha256:" + "6" * 64,
            activation_bundle_root=self.base / "activation-bundle",
            expected_semantic_baseline_id=RELEASE_C,
            allow_first_deploy=True,
            approval_note="approved reviewed activation",
            runner=runner,
        )
        with self.assertRaisesRegex(promote.PromotionError, "first-deploy-approval"):
            instance.run()
        self.assertEqual(runner.calls, [])

    def test_invalid_activation_bundle_blocks_before_any_runner_call(self):
        runner = ResultRunner(promote.RunResult(("unexpected",), 0, b"", b""))
        instance = promote.BrainPromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=self.base / "store" / ("a" * 64),
            public_baseline_id=BASELINE_ID,
            public_baseline_root=self.base / "baseline" / ("d" * 64),
            activation_bundle_id="sha256:" + "6" * 64,
            activation_bundle_root=self.base / "missing-activation-bundle",
            expected_semantic_baseline_id=RELEASE_B,
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="execute",
            approval_note="fixture approval",
            runner=runner,
        )
        with self.assertRaisesRegex(promote.PromotionError, "activation bundle verification"):
            instance.run()
        self.assertEqual(runner.calls, [])

    def test_worker_bundle_dry_run_uses_explicit_checkout_entry_with_copied_config(self):
        public_dir = self.base / "public-smoke"
        public_dir.mkdir()
        (public_dir / "asset.txt").write_text("asset\n", encoding="utf-8")
        bundle_dir = self.base / "bundle-smoke"
        deploy_config = self.base / "copied-wrangler.jsonc"
        deploy_config.write_text("{}\n", encoding="utf-8")

        class BundleRunner(promote.CommandRunner):
            def __init__(inner):
                inner.calls = []
                inner.environments = []

            def run(inner, args, *, cwd, timeout=None, env=None):
                del cwd, timeout
                command = tuple(str(value) for value in args)
                inner.calls.append(command)
                inner.environments.append(dict(env or {}))
                if Path(command[0]).name in {"npm", "npm-cli.js"} and command[1:] == (
                    "run",
                    "typecheck",
                ):
                    return promote.RunResult(command, 0, b"", b"")
                if Path(command[0]).name in {"npm", "npm-cli.js"} and command[1:] == (
                    "run",
                    "test:unit",
                ):
                    return promote.RunResult(command, 0, b"", b"")
                output = Path(command[command.index("--outdir") + 1])
                output.mkdir(parents=True)
                (output / "index.js").write_text("export default {};\n", encoding="utf-8")
                return promote.RunResult(command, 0, b"", b"")

        runner = BundleRunner()
        instance = self.promoter(runner=runner)
        tools = node_executables_identity(self.base)
        tools["wrangler"]["path"] = str(
            self.repo / "wiki" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
        )
        instance._bind_node_executables(tools)
        entry, _ = instance._run_worker_checks(
            public_dir, bundle_dir, deploy_config
        )
        self.assertEqual(entry, bundle_dir / "index.js")
        first_wrangler = runner.calls[2]
        self.assertEqual(Path(first_wrangler[0]).name, "node")
        self.assertEqual(
            first_wrangler[1:4],
            (
                str(self.repo / "wiki" / "node_modules/wrangler/bin/wrangler.js"),
                "deploy",
                str(self.repo / "wiki" / "src" / "index.ts"),
            ),
        )
        self.assertEqual(
            first_wrangler[first_wrangler.index("--config") + 1],
            str(deploy_config),
        )
        self.assertTrue(
            all("CLOUDFLARE_API_TOKEN" not in call_env for call_env in runner.environments)
        )

    def test_history_evidence_preserves_raw_wrangler_bodies(self):
        bodies = [b'[{"id":"deployment"}]\n', b'[{"id":"version"}]\n']

        class HistoryRunner(promote.CommandRunner):
            def __init__(inner):
                inner.index = 0

            def run(inner, args, *, cwd, timeout=None, env=None):
                del cwd, timeout, env
                body = bodies[inner.index]
                inner.index += 1
                return promote.RunResult(tuple(args), 0, body, b"")

        instance = self.promoter(runner=HistoryRunner())
        tools = node_executables_identity(self.base)
        tools["wrangler"]["path"] = str(
            self.repo / "wiki" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
        )
        instance._bind_node_executables(tools)
        evidence, raw = instance._history_evidence()
        self.assertEqual(raw, {"deployments": bodies[0], "versions": bodies[1]})
        for key in ("deployments", "versions"):
            self.assertEqual(evidence[key]["sha256"], hashlib.sha256(raw[key]).hexdigest())
            self.assertEqual(evidence[key]["bytes"], len(raw[key]))

    def test_selector_404_requires_explicit_exception(self):
        probe = selector_probe(status=404, body=b"missing")
        with self.assertRaisesRegex(promote.PromotionError, "selector is absent"):
            promote.selector_from_probe(
                probe,
                RELEASE_A,
                allow_first_deploy=False,
            )
        state = promote.selector_from_probe(
            probe,
            RELEASE_A,
            allow_first_deploy=True,
        )
        self.assertEqual(state.status, 404)

    def test_first_deploy_flag_is_rejected_when_selector_exists(self):
        with self.assertRaisesRegex(promote.PromotionError, "production has a selector"):
            promote.selector_from_probe(
                selector_probe(),
                RELEASE_A,
                allow_first_deploy=True,
            )

    def test_v1_selector_cannot_discard_same_logical_release_without_manifest_proof(self):
        with self.assertRaisesRegex(
            promote.PromotionError,
            "cannot distinguish the live manifest",
        ):
            promote.selector_from_probe(
                selector_probe(RELEASE_A),
                RELEASE_A,
                candidate_manifest_sha256="3" * 64,
                allow_first_deploy=False,
            )

    def test_release_root_must_match_id_and_reject_symlink(self):
        release = make_release(self.base, RELEASE_A)
        self.assertEqual(self.promoter()._validate_release_root(release.root, RELEASE_A), release.root)
        with self.assertRaisesRegex(promote.PromotionError, "requested release ID"):
            self.promoter()._validate_release_root(release.root, RELEASE_B)
        alias = self.base / "release-alias"
        alias.symlink_to(release.root, target_is_directory=True)
        with self.assertRaisesRegex(promote.PromotionError, "symlink"):
            self.promoter()._validate_release_root(alias, RELEASE_A)

        legacy_root = self.base / "legacy-store" / RELEASE_A.removeprefix("sha256:")
        shutil.copytree(release.root, legacy_root)
        with self.assertRaisesRegex(promote.PromotionError, "manifest digest"):
            self.promoter()._validate_release_root(legacy_root, RELEASE_A)
        self.assertEqual(
            self.promoter()._validate_release_root(
                legacy_root,
                RELEASE_A,
                allow_legacy_release_id_root=True,
            ),
            legacy_root,
        )

    def test_deployment_status_rejects_split_and_noninteger_percentage(self):
        for percentage in (100.0, True):
            value = {"id": DEPLOYMENT_A, "versions": [{"version_id": VERSION_A, "percentage": percentage}]}
            with self.assertRaisesRegex(promote.PromotionError, "100%"):
                promote.parse_deployment_status(json.dumps(value).encode())
        value = {
            "id": DEPLOYMENT_A,
            "versions": [
                {"version_id": VERSION_A, "percentage": 50},
                {"version_id": VERSION_B, "percentage": 50},
            ],
        }
        with self.assertRaisesRegex(promote.PromotionError, "unambiguous"):
            promote.parse_deployment_status(json.dumps(value).encode())

    def test_wrong_main_and_dirty_checkout_fail(self):
        git_dir = self.repo / ".git-fixture"

        class GitRunner(promote.CommandRunner):
            def __init__(
                self,
                *,
                head=COMMIT,
                main=COMMIT,
                branch="",
                dirty=False,
            ):
                self.head = head
                self.main = main
                self.branch = branch
                self.dirty = dirty

            def run(inner, args, *, cwd, timeout=None, env=None):
                del cwd, timeout
                self.assertIsNotNone(env)
                self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
                self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
                self.assertNotIn("GIT_DIR", env)
                tail = tuple(args[3:])
                if tail == ("rev-parse", "--show-toplevel"):
                    output = str(self.repo)
                elif tail == ("rev-parse", "HEAD"):
                    output = inner.head
                elif tail == ("rev-parse", "refs/heads/main"):
                    output = inner.main
                elif tail == ("symbolic-ref", "--quiet", "--short", "HEAD"):
                    if not inner.branch:
                        return promote.RunResult(tuple(args), 1, b"", b"")
                    output = inner.branch
                elif tail == ("status", "--porcelain=v1", "--untracked-files=all"):
                    output = " M wiki/src/index.ts" if inner.dirty else ""
                elif tail == ("rev-parse", "--absolute-git-dir"):
                    output = str(git_dir)
                else:
                    raise AssertionError(tail)
                return promote.RunResult(tuple(args), 0, (output + "\n").encode(), b"")

        with self.assertRaisesRegex(promote.PromotionError, "must equal HEAD"):
            self.promoter(runner=GitRunner(main="d" * 40))._check_git_authority(COMMIT)
        with self.assertRaisesRegex(promote.PromotionError, "checkout is dirty"):
            self.promoter(runner=GitRunner(dirty=True))._check_git_authority(COMMIT)

        current = "d" * 40
        self.assertEqual(
            self.promoter(
                runner=GitRunner(head=current, main=current, branch="main")
            )._check_recovery_checkout(COMMIT),
            current,
        )
        self.assertEqual(
            self.promoter(
                runner=GitRunner(head=COMMIT, main=current, branch="")
            )._check_recovery_checkout(COMMIT),
            COMMIT,
        )
        with self.assertRaisesRegex(promote.PromotionError, "clean current main"):
            self.promoter(
                runner=GitRunner(head=current, main=current, branch="feature")
            )._check_recovery_checkout(COMMIT)

    def test_selector_race_aborts_preparation(self):
        candidate = make_release(self.base, RELEASE_A)
        baseline = make_baseline(self.base)
        states = iter(
            [
                (selector_probe(status=404, body=b"missing-a"), "fixture-ca"),
                (selector_probe(RELEASE_A), "fixture-ca"),
            ]
        )

        class RacePromoter(promote.BrainPromoter):
            def _verify_release(
                inner,
                expected_release_id,
                root_input,
                *,
                allow_legacy_release_id_root=False,
            ):
                del expected_release_id, root_input, allow_legacy_release_id_root
                return candidate

            def _check_git_authority(inner, expected_commit):
                return expected_commit

            def _verify_toolchain(inner):
                return "v22.0.0", "4.120.0"

            def _wrangler_installation_identity(inner):
                return node_modules_identity(self.base)

            def _node_executables_identity(inner):
                return node_executables_identity(self.base)

            def _probe_selector(inner, phase):
                del phase
                return next(states)

            def _stage_public(inner, candidate_info, prior, public_baseline, public_dir):
                del prior, public_baseline
                public_dir.mkdir()
                (public_dir / "brain.html").write_bytes(
                    (candidate_info.root / "site/out/brain.html").read_bytes()
                )
                staged = promote.selector_from_probe(
                    selector_probe(RELEASE_A),
                    RELEASE_A,
                    allow_first_deploy=False,
                )
                return {"schema": "wikilean.public-build-result/v1"}, staged

            def _run_worker_checks(inner, public_dir, bundle_dir, deploy_config):
                del public_dir, deploy_config
                bundle_dir.mkdir()
                entry = bundle_dir / "index.js"
                entry.write_text("export default {};\n", encoding="utf-8")
                return entry, promote.inventory_tree(bundle_dir)

            def _history_evidence(inner, config=None):
                del config
                return {}, {"deployments": b"[]\n", "versions": b"[]\n"}

            def _wrangler_status(inner, config=None):
                del config
                return promote.DeploymentState(DEPLOYMENT_A, VERSION_A, "x" * 64), promote.RunResult((), 0, b"{}", b"")

        instance = RacePromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=candidate.root,
            public_baseline_id=baseline.baseline_id,
            public_baseline_root=baseline.root,
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="dry-run",
            allow_first_deploy=True,
            attempt_id="race-attempt",
        )
        with mock.patch.object(promote, "verify_public_baseline", return_value=baseline):
            with self.assertRaisesRegex(promote.PromotionError, "selector changed"):
                instance.prepare()


class BrainPromotionJournalFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.repo = make_repo(self.base)
        self.receipt = initialize_target_receipt_root(
            self.base / "receipts", self.repo, "https://example.test"
        )
        self.prepared = with_reviewed_activation(make_prepared(self.base))

    def tearDown(self):
        self.temp.cleanup()

    def run_with_journal(
        self,
        promoter_instance,
        *,
        prepared: promote.PreparedPromotion | None = None,
        attempt_id: str = "attempt-fixture",
    ):
        prepared = prepared or self.prepared
        with PromotionLock(self.receipt):
            journal = EventJournal.create_with_intent(
                self.receipt,
                attempt_id,
                promote.journal_safe(promoter_instance._intent_payload(prepared)),
            )
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = promoter_instance._deploy(prepared, journal)
            return result, EventJournal.load(journal.attempt_dir)

    def run_reconciliation(
        self,
        scenario: ReconcileScenario,
        attempt_id: str,
        *,
        prepared: promote.PreparedPromotion | None = None,
        legacy_selector_intent: bool = False,
    ):
        prepared = prepared or self.prepared
        intent = scenario._intent_payload(prepared)
        if legacy_selector_intent:
            intent["staged_selector"].pop("manifest_sha256")
            intent["staged_selector"].pop("previous_manifest_sha256")
            intent["predeploy"].pop("manifest_sha256")
            intent["predeploy"].pop("previous_manifest_sha256")
        with PromotionLock(self.receipt):
            journal = EventJournal.create_with_intent(
                self.receipt,
                attempt_id,
                promote.journal_safe(intent),
            )
            if scenario.deploy_invocation_started:
                journal.append(
                    "deploy_invocation",
                    {"production_mutation_possible_after_this_event": True},
                )
            with mock.patch.object(
                promote,
                "verify_public_baseline",
                return_value=prepared.public_baseline,
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    result = scenario.reconcile(journal)
            return result, EventJournal.load(journal.attempt_dir)

    def test_legacy_v1_intent_remains_reconcilable(self):
        legacy_body = (
            json.dumps(
                {
                    "schema": "wikilean.release-selector/v1",
                    "release_id": RELEASE_A,
                    "release": RELEASE_A.removeprefix("sha256:"),
                    "manifest": (
                        f"/assets/brain/releases/{RELEASE_A.removeprefix('sha256:')}/release.json"
                    ),
                    "previous_release_id": RELEASE_B,
                    "previous_release": RELEASE_B.removeprefix("sha256:"),
                    "previous_manifest": (
                        f"/assets/brain/releases/{RELEASE_B.removeprefix('sha256:')}/release.json"
                    ),
                    "audited_at": self.prepared.audited_at,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        legacy_selector = promote.selector_from_probe(
            selector_probe(RELEASE_A, body=legacy_body),
            RELEASE_A,
            allow_first_deploy=False,
        )
        prepared = replace(
            self.prepared,
            staged_selector=legacy_selector,
            initial_selector=legacy_selector,
            predeploy_release=self.prepared.candidate,
        )
        annotations = {
            "workers/tag": prepared.tag,
            "workers/message": prepared.message,
        }
        observed = make_observation(legacy_selector, annotations=annotations)
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            prepared,
            [observed, observed],
            reconcile_attempt="legacy-selector-intent",
        )
        exit_code, journal = self.run_reconciliation(
            scenario,
            "legacy-selector-intent",
            prepared=prepared,
            legacy_selector_intent=True,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "deployed_reconciled")

    def test_execute_publishes_no_empty_attempt_and_exactly_one_intent(self):
        work = self.base / "atomic-work"
        prepared = with_reviewed_activation(
            replace(make_prepared(work), attempt_id="atomic-attempt")
        )

        class AtomicPromoter(promote.BrainPromoter):
            def _verify_reviewed_activation(inner, expected=None):
                del expected
                return prepared.activation

            def _prepare_reviewed_activation(inner, activation):
                self.assertEqual(activation, prepared.activation)
                return prepared

            def _deploy(inner, prepared_value, journal):
                self.assertEqual(prepared_value, prepared)
                self.assertEqual([event["kind"] for event in journal.events], ["intent"])
                journal.append("final_state", {"outcome": "fixture_complete"})
                return 0

        instance = AtomicPromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=prepared.candidate.root,
            public_baseline_id=prepared.public_baseline.baseline_id,
            public_baseline_root=prepared.public_baseline.root,
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="execute",
            approval_note="fixture approval",
            activation_bundle_id=prepared.activation.bundle_id,
            activation_bundle_root=prepared.activation.root,
            expected_semantic_baseline_id=(
                prepared.activation.semantic_baseline_release_id
            ),
            attempt_id="atomic-attempt",
            audited_at=prepared.audited_at,
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(instance.run(), 0)
        journal = EventJournal.load(self.receipt / "attempts" / "atomic-attempt")
        self.assertEqual(
            [event["kind"] for event in journal.events], ["intent", "final_state"]
        )
        self.assertEqual(
            sum(event["kind"] == "intent" for event in journal.events),
            1,
        )
        pending = [
            path.name
            for path in (self.receipt / "attempts").iterdir()
            if path.name.startswith(".pending-attempt-")
        ]
        self.assertEqual(pending, [])

    def test_nonzero_deploy_is_adopted_only_after_annotation_and_canary_proof(self):
        result = promote.RunResult(("wrangler", "deploy"), 1, b"Current Version ID: " + VERSION_B.encode() + b"\n", b"timeout")
        runner = ResultRunner(result)
        scenario = DeployScenario(self.repo, self.receipt, runner)
        exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 0)
        self.assertTrue(journal.terminal)
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "deployed_after_uncertain_command")
        deploy_event = next(event for event in journal.events if event["kind"] == "deploy_result")
        self.assertEqual(deploy_event["payload"]["command"]["returncode"], 1)
        intent = journal.events[0]["payload"]
        self.assertEqual(
            intent["activation_bundle"]["bundle_id"],
            self.prepared.activation.bundle_id,
        )
        invocation = next(
            event for event in journal.events if event["kind"] == "deploy_invocation"
        )
        self.assertEqual(
            invocation["payload"]["activation_bundle"],
            self.prepared.activation.reference(),
        )
        self.assertEqual(len(runner.calls), 1)
        command = runner.calls[0]
        self.assertEqual(Path(command[0]).name, "node")
        self.assertEqual(command[2:4], ("deploy", str(self.prepared.bundle_entry)))
        self.assertEqual(command[command.index("--config") + 1], str(self.prepared.deploy_config))
        self.assertIn("--no-bundle", command)
        self.assertEqual(command[command.index("--assets") + 1], str(self.prepared.public_dir))
        for sealed_path in (
            self.prepared.bundle_entry,
            self.prepared.deploy_config,
            self.prepared.public_dir,
        ):
            with self.assertRaises(ValueError):
                sealed_path.relative_to(self.repo)

    def test_final_fence_aborts_on_public_tree_mutation_before_wrangler(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        scenario = FenceScenario(self.repo, self.receipt, runner, self.prepared)
        (self.prepared.public_dir / "late-mutation.js").write_text(
            "unexpected\n", encoding="utf-8"
        )
        exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertTrue(journal.terminal)
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "predeploy_race_aborted")
        self.assertIn("sealed public tree changed", journal.events[-2]["payload"]["error"])

    def test_final_fence_aborts_on_wrangler_installation_tampering(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        scenario = FenceScenario(self.repo, self.receipt, runner, self.prepared)
        changed = dict(self.prepared.wrangler_installation)
        changed["sha256"] = "f" * 64
        scenario._wrangler_installation_identity = lambda: changed
        with mock.patch.object(
            promote, "verify_public_baseline", return_value=self.prepared.public_baseline
        ):
            exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn(
            "dependency tree changed",
            journal.events[-2]["payload"]["error"],
        )

    def test_final_fence_aborts_on_node_executable_tampering(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        scenario = FenceScenario(self.repo, self.receipt, runner, self.prepared)
        changed = copy.deepcopy(self.prepared.node_executables)
        changed["node"]["sha256"] = "f" * 64
        scenario._node_executables_identity = lambda: changed
        with mock.patch.object(
            promote, "verify_public_baseline", return_value=self.prepared.public_baseline
        ):
            exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn(
            "executable bytes changed",
            journal.events[-2]["payload"]["error"],
        )

    def test_immediate_fence_aborts_on_wrangler_mutation_before_spawn(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        probes = [
            (
                SelectorProbe(
                    body=self.prepared.initial_selector.body,
                    body_sha256=self.prepared.initial_selector.body_sha256,
                    content_type="application/json",
                    status=self.prepared.initial_selector.status,
                    trust_source=self.prepared.trust_source,
                    url=f"https://example.test/assets/brain/current.json?fence={index}",
                ),
                self.prepared.trust_source,
            )
            for index in (1, 2)
        ]
        scenario = FenceScenario(
            self.repo,
            self.receipt,
            runner,
            self.prepared,
            probes=probes,
            statuses=[self.prepared.predeploy] * 4,
        )
        checked_fence = scenario._remote_predeploy_fence
        changed = dict(self.prepared.wrangler_installation)
        changed["sha256"] = "f" * 64

        def mutate_after_fence(prepared, *, config, phase):
            result = checked_fence(prepared, config=config, phase=phase)
            if phase == "immediate-before-deploy":
                scenario._wrangler_installation_identity = lambda: changed
            return result

        scenario._remote_predeploy_fence = mutate_after_fence
        with mock.patch.object(
            promote, "verify_public_baseline", return_value=self.prepared.public_baseline
        ):
            exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertIn(
            "changed during the immediate predeploy fence",
            journal.events[-2]["payload"]["error"],
        )

    def test_final_fence_aborts_on_selector_race_before_wrangler(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        scenario = FenceScenario(
            self.repo,
            self.receipt,
            runner,
            self.prepared,
            probes=[(selector_probe(RELEASE_A), self.prepared.trust_source)],
        )
        with mock.patch.object(
            promote, "verify_public_baseline", return_value=self.prepared.public_baseline
        ):
            exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "predeploy_race_aborted")
        self.assertIn("selector changed", journal.events[-2]["payload"]["error"])

    def test_final_fence_aborts_on_status_race_before_wrangler(self):
        runner = ResultRunner(promote.RunResult(("deploy",), 0, b"", b""))
        changed = promote.DeploymentState(DEPLOYMENT_C, VERSION_C, "8" * 64)
        scenario = FenceScenario(
            self.repo,
            self.receipt,
            runner,
            self.prepared,
            statuses=[self.prepared.predeploy, changed],
        )
        with mock.patch.object(
            promote, "verify_public_baseline", return_value=self.prepared.public_baseline
        ):
            exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "predeploy_race_aborted")
        self.assertIn("deployment changed", journal.events[-2]["payload"]["error"])

    def test_final_fence_treats_changed_404_bytes_as_a_race(self):
        initial_body = b"missing-before"
        initial = promote.SelectorState(
            404,
            hashlib.sha256(initial_body).hexdigest(),
            initial_body,
            None,
            None,
            None,
            None,
        )
        prepared = replace(
            self.prepared,
            initial_selector=initial,
            prior=None,
            predeploy_release=None,
        )
        changed = selector_probe(status=404, body=b"missing-after")
        scenario = FenceScenario(
            self.repo,
            self.receipt,
            ResultRunner(promote.RunResult(("deploy",), 0, b"", b"")),
            prepared,
            probes=[(changed, prepared.trust_source)],
        )
        with self.assertRaisesRegex(promote.PromotionError, "selector changed"):
            scenario._remote_predeploy_fence(
                prepared,
                config=prepared.deploy_config,
                phase="404-race",
            )

    def test_candidate_reconciliation_requires_exact_identity_and_post_canary_fence(self):
        annotations = {
            "workers/tag": self.prepared.tag,
            "workers/message": self.prepared.message,
        }
        exact = make_observation(self.prepared.staged_selector, annotations=annotations)
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [exact, exact],
            reconcile_attempt="candidate-exact",
        )
        exit_code, journal = self.run_reconciliation(scenario, "candidate-exact")
        self.assertEqual(exit_code, 0)
        self.assertEqual(scenario.observation_calls, 2)
        self.assertEqual(
            scenario.canary_releases,
            [(RELEASE_A, "reconcile-candidate", self.prepared.trust_source)],
        )
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "deployed_reconciled")
        phases = [
            event["payload"].get("phase")
            for event in journal.events
            if event["kind"] == "observation"
        ]
        self.assertIn("reconciliation_post_canary", phases)

        bad_previous = promote.SelectorState(
            self.prepared.staged_selector.status,
            self.prepared.staged_selector.body_sha256,
            self.prepared.staged_selector.body,
            self.prepared.staged_selector.current_release_id,
            None,
            self.prepared.staged_selector.retained_release_id,
            self.prepared.staged_selector.audited_at,
        )
        bad_audit = promote.SelectorState(
            self.prepared.staged_selector.status,
            self.prepared.staged_selector.body_sha256,
            self.prepared.staged_selector.body,
            self.prepared.staged_selector.current_release_id,
            self.prepared.staged_selector.previous_release_id,
            self.prepared.staged_selector.retained_release_id,
            "2030-01-01T00:00:01Z",
        )
        mismatches = {
            "hash": make_observation(
                self.prepared.staged_selector,
                annotations=annotations,
                selector_sha256="9" * 64,
            ),
            "previous": make_observation(bad_previous, annotations=annotations),
            "audited": make_observation(bad_audit, annotations=annotations),
            "trust": make_observation(
                self.prepared.staged_selector,
                annotations=annotations,
                trust_source="different-ca",
            ),
        }
        for label, observation in mismatches.items():
            attempt_id = f"candidate-mismatch-{label}"
            mismatch = ReconcileScenario(
                self.repo,
                self.receipt,
                self.prepared,
                [observation],
                reconcile_attempt=attempt_id,
            )
            with self.subTest(label=label):
                with self.assertRaisesRegex(promote.PromotionError, "selector bytes"):
                    self.run_reconciliation(mismatch, attempt_id)
                loaded = EventJournal.load(self.receipt / "attempts" / attempt_id)
                self.assertTrue(loaded.incomplete)
                self.assertNotIn("final_state", [event["kind"] for event in loaded.events])

    def test_candidate_reconciliation_rejects_post_canary_state_change(self):
        annotations = {
            "workers/tag": self.prepared.tag,
            "workers/message": self.prepared.message,
        }
        before = make_observation(self.prepared.staged_selector, annotations=annotations)
        after = make_observation(
            self.prepared.staged_selector,
            deployment=promote.DeploymentState(DEPLOYMENT_C, VERSION_C, "8" * 64),
            annotations=annotations,
        )
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [before, after],
            reconcile_attempt="candidate-post-canary-race",
        )
        with self.assertRaisesRegex(promote.PromotionError, "changed after"):
            self.run_reconciliation(scenario, "candidate-post-canary-race")
        loaded = EventJournal.load(
            self.receipt / "attempts" / "candidate-post-canary-race"
        )
        self.assertTrue(loaded.incomplete)
        self.assertNotIn("final_state", [event["kind"] for event in loaded.events])

    def test_exact_prior_reconciliation_uses_digest_quiet_and_post_canary_fences(self):
        exact_prior = make_observation(
            self.prepared.initial_selector,
            deployment=self.prepared.predeploy,
            selector_sha256=self.prepared.initial_selector.body_sha256,
        )
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [exact_prior, exact_prior, exact_prior],
            reconcile_attempt="prior-exact",
            reconcile_quiet_seconds=7,
            confirm_no_production_change=True,
            no_change_approval="Jack confirmed no production change",
            deploy_invocation_started=True,
            command_timeout=7,
        )
        exit_code, journal = self.run_reconciliation(scenario, "prior-exact")
        self.assertEqual(exit_code, 0)
        self.assertEqual(scenario.sleeps, [7])
        self.assertEqual(scenario.observation_calls, 3)
        self.assertEqual(
            scenario.canary_releases,
            [(RELEASE_B, "reconcile-prior", self.prepared.trust_source)],
        )
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "no_production_change")
        phases = [
            event["payload"].get("phase")
            for event in journal.events
            if event["kind"] == "observation"
        ]
        self.assertIn("prior_state_quiet_fence", phases)
        self.assertIn("prior_state_post_canary", phases)

    def test_no_change_reconciliation_rejects_attempt_deployment_history(self):
        exact_prior = make_observation(
            self.prepared.initial_selector,
            deployment=self.prepared.predeploy,
            selector_sha256=self.prepared.initial_selector.body_sha256,
        )
        correlated = {
            "version_ids": [VERSION_B],
            "deployments": [
                {"deployment_id": DEPLOYMENT_B, "version_ids": [VERSION_B]}
            ],
            "versions_returned": 2,
            "deployments_returned": 1,
            "versions_sha256": "1" * 64,
            "deployments_sha256": "2" * 64,
        }
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [exact_prior, exact_prior],
            reconcile_attempt="prior-history-present",
            reconcile_quiet_seconds=5,
            confirm_no_production_change=True,
            no_change_approval="Jack confirmed no production change",
            deploy_invocation_started=True,
            command_timeout=5,
            attempt_histories=[correlated, correlated],
        )
        with self.assertRaisesRegex(promote.PromotionError, "history contains"):
            self.run_reconciliation(scenario, "prior-history-present")
        loaded = EventJournal.load(
            self.receipt / "attempts" / "prior-history-present"
        )
        self.assertTrue(loaded.incomplete)

    def test_no_change_reconciliation_records_stable_orphan_version(self):
        exact_prior = make_observation(
            self.prepared.initial_selector,
            deployment=self.prepared.predeploy,
            selector_sha256=self.prepared.initial_selector.body_sha256,
        )
        orphan = {
            "version_ids": [VERSION_B],
            "deployments": [],
            "versions_returned": 2,
            "deployments_returned": 1,
            "versions_sha256": "1" * 64,
            "deployments_sha256": "2" * 64,
        }
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [exact_prior, exact_prior, exact_prior],
            reconcile_attempt="prior-orphan-version",
            reconcile_quiet_seconds=5,
            confirm_no_production_change=True,
            no_change_approval="Jack confirmed no production change",
            deploy_invocation_started=True,
            command_timeout=5,
            attempt_histories=[orphan, orphan],
        )
        exit_code, journal = self.run_reconciliation(
            scenario, "prior-orphan-version"
        )
        self.assertEqual(exit_code, 0)
        final = journal.events[-1]["payload"]
        self.assertEqual(final["outcome"], "no_production_change")
        self.assertEqual(
            final["attempt_history_after_quiet"]["version_ids"], [VERSION_B]
        )

    def test_retained_release_may_come_from_an_older_commit(self):
        older_prior = replace(
            self.prepared.prior,
            authority_commit=PRIOR_COMMIT,
            reducer_commit=PRIOR_COMMIT,
        )
        prepared = replace(
            self.prepared,
            prior=older_prior,
            predeploy_release=older_prior,
        )
        exact_prior = make_observation(
            prepared.initial_selector,
            deployment=prepared.predeploy,
            selector_sha256=prepared.initial_selector.body_sha256,
        )
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            prepared,
            [exact_prior, exact_prior, exact_prior],
            reconcile_attempt="retained-older-commit",
        )
        with PromotionLock(self.receipt):
            journal = EventJournal.create_with_intent(
                self.receipt,
                "retained-older-commit",
                promote.journal_safe(scenario._intent_payload(prepared)),
            )
            with mock.patch.object(
                promote,
                "verify_public_baseline",
                return_value=prepared.public_baseline,
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    exit_code = scenario.reconcile(journal)
        self.assertEqual(exit_code, 0)
        loaded = EventJournal.load(journal.attempt_dir)
        self.assertEqual(
            loaded.events[-1]["payload"]["outcome"],
            "aborted_before_deploy_reconciled",
        )

    def test_prior_digest_mismatch_remains_incomplete(self):
        mismatched = make_observation(
            self.prepared.initial_selector,
            deployment=self.prepared.predeploy,
            selector_sha256="9" * 64,
        )
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [mismatched],
            reconcile_attempt="prior-digest-mismatch",
        )
        with self.assertRaisesRegex(promote.PromotionError, "neither.*exact prior"):
            self.run_reconciliation(scenario, "prior-digest-mismatch")
        loaded = EventJournal.load(self.receipt / "attempts" / "prior-digest-mismatch")
        self.assertTrue(loaded.incomplete)
        self.assertNotIn("final_state", [event["kind"] for event in loaded.events])

    def test_missing_selector_digest_mismatch_is_not_exact_prior_state(self):
        prior_body = b"missing-before"
        missing = promote.SelectorState(
            404,
            hashlib.sha256(prior_body).hexdigest(),
            prior_body,
            None,
            None,
            None,
            None,
        )
        prepared = replace(
            self.prepared,
            initial_selector=missing,
            prior=None,
            predeploy_release=None,
        )
        changed = make_observation(
            None,
            deployment=prepared.predeploy,
            selector_sha256=hashlib.sha256(b"missing-after").hexdigest(),
        )
        scenario = ReconcileScenario(
            self.repo,
            self.receipt,
            prepared,
            [changed],
            reconcile_attempt="missing-selector-digest-mismatch",
        )
        with PromotionLock(self.receipt):
            journal = EventJournal.create_with_intent(
                self.receipt,
                "missing-selector-digest-mismatch",
                promote.journal_safe(scenario._intent_payload(prepared)),
            )
            with mock.patch.object(
                promote,
                "verify_public_baseline",
                return_value=prepared.public_baseline,
            ):
                with self.assertRaisesRegex(promote.PromotionError, "neither.*exact prior"):
                    scenario.reconcile(journal)
        loaded = EventJournal.load(journal.attempt_dir)
        self.assertTrue(loaded.incomplete)

    def test_external_supersession_requires_explicit_approval_and_stability(self):
        external_state = promote.selector_from_probe(
            selector_probe(RELEASE_C),
            RELEASE_C,
            allow_first_deploy=False,
        )
        external = make_observation(
            external_state,
            deployment=promote.DeploymentState(DEPLOYMENT_C, VERSION_C, "8" * 64),
        )
        missing_approval = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [],
            reconcile_attempt="external-missing-approval",
            accept_external_supersession=True,
        )
        with self.assertRaisesRegex(
            promote.PromotionError, "external supersession requires"
        ):
            missing_approval._validate_options()

        blocked = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [external],
            reconcile_attempt="external-blocked",
        )
        with self.assertRaisesRegex(promote.PromotionError, "neither.*exact prior"):
            self.run_reconciliation(blocked, "external-blocked")
        blocked_journal = EventJournal.load(
            self.receipt / "attempts" / "external-blocked"
        )
        self.assertTrue(blocked_journal.incomplete)

        approved = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [external, external],
            reconcile_attempt="external-approved",
            reconcile_quiet_seconds=5,
            deploy_invocation_started=True,
            command_timeout=5,
            accept_external_supersession=True,
            external_supersession_approval="Jack approved external supersession",
        )
        approved._validate_options()
        exit_code, journal = self.run_reconciliation(approved, "external-approved")
        self.assertEqual(exit_code, 0)
        self.assertEqual(approved.sleeps, [5])
        self.assertEqual(approved.observation_calls, 2)
        self.assertEqual(journal.events[-1]["payload"]["outcome"], "externally_superseded")
        self.assertEqual(
            journal.events[-1]["payload"]["external_supersession_approval"],
            "Jack approved external supersession",
        )
        phases = [
            event["payload"].get("phase")
            for event in journal.events
            if event["kind"] == "observation"
        ]
        self.assertIn("external_supersession_history_before_quiet", phases)
        self.assertIn("external_supersession_history_after_quiet", phases)

        too_short = ReconcileScenario(
            self.repo,
            self.receipt,
            self.prepared,
            [external],
            reconcile_attempt="external-too-short",
            reconcile_quiet_seconds=4,
            deploy_invocation_started=True,
            command_timeout=5,
            accept_external_supersession=True,
            external_supersession_approval="Jack approved external supersession",
        )
        with self.assertRaisesRegex(promote.PromotionError, "recorded command timeout"):
            self.run_reconciliation(too_short, "external-too-short")

    def test_crash_after_intent_leaves_reconcilable_incomplete_attempt(self):
        scenario = DeployScenario(self.repo, self.receipt, ResultRunner(InjectedCrash()))
        with PromotionLock(self.receipt):
            journal = EventJournal.create_with_intent(
                self.receipt,
                "attempt-fixture",
                promote.journal_safe(scenario._intent_payload(self.prepared)),
            )
            with self.assertRaises(InjectedCrash):
                scenario._deploy(self.prepared, journal)
            loaded = EventJournal.load(journal.attempt_dir)
            self.assertTrue(loaded.incomplete)
            self.assertEqual(
                [event["kind"] for event in loaded.events],
                ["intent", "observation", "deploy_invocation"],
            )
        blocked = promote.BrainPromoter(
            repo_root=self.repo,
            python=Path(sys.executable),
            release_id=RELEASE_A,
            release_root=self.prepared.candidate.root,
            public_baseline_id=self.prepared.public_baseline.baseline_id,
            public_baseline_root=self.prepared.public_baseline.root,
            receipt_root=self.receipt,
            base_url="https://example.test",
            production_origin="https://example.test",
            mode="dry-run",
            attempt_id="later-attempt",
        )
        with self.assertRaisesRegex(promote.PromotionError, "block new work"):
            blocked.run()

    def test_failed_canary_leaves_attempt_incomplete_and_blocks_new_mutation(self):
        result = promote.RunResult(("wrangler", "deploy"), 0, b"Current Version ID: " + VERSION_B.encode() + b"\n", b"")
        scenario = DeployScenario(self.repo, self.receipt, ResultRunner(result))
        scenario.canary_ok = False
        exit_code, journal = self.run_with_journal(scenario)
        self.assertEqual(exit_code, 1)
        self.assertTrue(journal.incomplete)
        self.assertNotIn("final_state", [event["kind"] for event in journal.events])

class BrainPromotionDryRunTest(unittest.TestCase):
    def test_reviewed_activation_reuses_exact_retained_deployment_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            selector_path = prepared.public_dir / "assets" / "brain" / "current.json"
            selector_path.parent.mkdir(parents=True)
            selector_path.write_bytes(prepared.staged_selector.body)
            prepared = replace(
                prepared,
                public_inventory=promote.inventory_tree(prepared.public_dir),
            )
            dry_run_promoter = promote.BrainPromoter(
                repo_root=repo,
                python=Path(sys.executable),
                release_id=prepared.candidate.release_id,
                release_root=prepared.candidate.root,
                public_baseline_id=prepared.public_baseline.baseline_id,
                public_baseline_root=prepared.public_baseline.root,
                receipt_root=receipt,
                base_url="https://example.test",
                production_origin="https://example.test",
                mode="dry-run",
                attempt_id=prepared.attempt_id,
                audited_at=prepared.audited_at,
            )
            retained = promote.retain_dry_run_artifacts(
                prepared,
                base / "retained",
                repo_root=repo,
                receipt_root=receipt,
            )
            intent = dry_run_promoter._retained_intent_payload(prepared, retained)
            activation = promote.ReviewedActivation(
                base / ("6" * 64),
                base / ("6" * 64) / "manifest.json",
                "7" * 64,
                "sha256:" + "6" * 64,
                RELEASE_C,
                prepared.candidate.release_id,
                prepared.public_baseline.baseline_id,
                prepared.candidate.authority_commit,
                prepared.candidate.reducer_commit,
                prepared.attempt_id,
                intent,
                retained,
            )

            class ReviewedPromoter(promote.BrainPromoter):
                def _verify_release(
                    inner,
                    expected_release_id,
                    root_input,
                *,
                allow_legacy_release_id_root=False,
            ):
                    if (
                        expected_release_id == prepared.candidate.release_id
                        and Path(root_input) == prepared.candidate.root
                    ):
                        return prepared.candidate
                    if (
                        prepared.prior is not None
                        and expected_release_id == prepared.prior.release_id
                        and Path(root_input) == prepared.prior.root
                    ):
                        self.assertTrue(allow_legacy_release_id_root)
                        return prepared.prior
                    raise AssertionError((expected_release_id, root_input))

                def _check_git_authority(inner, expected_commit):
                    self.assertEqual(expected_commit, COMMIT)
                    return expected_commit

                def _verify_toolchain(inner):
                    return prepared.node_version, prepared.wrangler_version

                def _wrangler_installation_identity(inner):
                    return prepared.wrangler_installation

                def _node_executables_identity(inner):
                    return prepared.node_executables

            instance = ReviewedPromoter(
                repo_root=repo,
                python=Path(sys.executable),
                release_id=prepared.candidate.release_id,
                release_root=prepared.candidate.root,
                public_baseline_id=prepared.public_baseline.baseline_id,
                public_baseline_root=prepared.public_baseline.root,
                activation_bundle_id=activation.bundle_id,
                activation_bundle_root=activation.root,
                expected_semantic_baseline_id=(
                    activation.semantic_baseline_release_id
                ),
                receipt_root=receipt,
                base_url="https://example.test",
                production_origin="https://example.test",
                mode="execute",
                approval_note="approved reviewed activation",
            )
            with mock.patch.object(
                promote,
                "verify_public_baseline",
                return_value=prepared.public_baseline,
            ):
                executable = instance._prepare_reviewed_activation(activation)
            self.assertEqual(executable.public_dir, retained.public_dir)
            self.assertEqual(executable.bundle_entry, retained.worker_entry)
            self.assertEqual(executable.deploy_config, retained.config)
            self.assertEqual(executable.activation, activation)
            self.assertEqual(instance.attempt_id, prepared.attempt_id)

    def test_retained_publication_failure_removes_candidate_and_preserves_recreated_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(base / "receipts", repo, "https://example.test")
            prepared = prepared_for_retention(base / "workspace")
            store = base / "retained"
            publish = promote._publish_no_replace

            def fail_after_publish(descriptor, source, target):
                publish(descriptor, source, target)
                os.mkdir(source, mode=0o700, dir_fd=descriptor)
                (store / source / "keep").write_text("unrelated")
                raise OSError("injected retention publication failure")

            with mock.patch.object(promote, "_publish_no_replace", side_effect=fail_after_publish):
                with self.assertRaisesRegex(OSError, "retention publication failure"):
                    promote.retain_dry_run_artifacts(prepared, store, repo_root=repo, receipt_root=receipt)
            survivors = [path for path in store.iterdir() if path.is_dir()]
            self.assertEqual(len(survivors), 1)
            self.assertTrue(survivors[0].name.startswith(".pending-"))
            self.assertEqual((survivors[0] / "keep").read_text(), "unrelated")

    def test_retained_publication_preserves_an_empty_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(base / "receipts", repo, "https://example.test")
            prepared = prepared_for_retention(base / "workspace")
            store = base / "retained"
            publish = promote._publish_no_replace
            existing = []

            def collide(descriptor, source, target):
                os.mkdir(target, mode=0o700, dir_fd=descriptor)
                existing.append(os.stat(target, dir_fd=descriptor).st_ino)
                publish(descriptor, source, target)

            with mock.patch.object(promote, "_publish_no_replace", side_effect=collide):
                with self.assertRaises(promote.PromotionError):
                    promote.retain_dry_run_artifacts(prepared, store, repo_root=repo, receipt_root=receipt)
            survivors = [path for path in store.iterdir() if path.is_dir()]
            self.assertEqual(len(survivors), 1)
            self.assertEqual(survivors[0].stat().st_ino, existing[0])
            self.assertEqual(list(survivors[0].iterdir()), [])

    def test_dry_run_writes_no_attempt_and_never_calls_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = make_prepared(base)

            class DryPromoter(promote.BrainPromoter):
                def prepare(inner):
                    return prepared

                def _deploy(inner, prepared_value, journal):
                    raise AssertionError((prepared_value, journal))

            instance = DryPromoter(
                repo_root=repo,
                python=Path(sys.executable),
                release_id=RELEASE_A,
                release_root=prepared.candidate.root,
                public_baseline_id=prepared.public_baseline.baseline_id,
                public_baseline_root=prepared.public_baseline.root,
                receipt_root=receipt,
                base_url="https://example.test",
                production_origin="https://example.test",
                mode="dry-run",
                attempt_id="dry-run-attempt",
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(instance.run(), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["schema"], promote.DRY_RUN_SCHEMA)
            self.assertFalse(value["production_mutated"])
            self.assertIsNone(value["proposed_intent"]["approval_note"])
            self.assertIsNone(value["proposed_intent"]["first_deploy_approval"])
            attempts = receipt.resolve() / "attempts"
            self.assertFalse(attempts.exists())

    def test_retained_dry_run_is_atomic_read_only_and_rebases_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            retained_store = base / "retained"

            class DryPromoter(promote.BrainPromoter):
                def prepare(inner):
                    return prepared

                def _deploy(inner, prepared_value, journal):
                    raise AssertionError((prepared_value, journal))

            instance = DryPromoter(
                repo_root=repo,
                python=Path(sys.executable),
                release_id=RELEASE_A,
                release_root=prepared.candidate.root,
                public_baseline_id=prepared.public_baseline.baseline_id,
                public_baseline_root=prepared.public_baseline.root,
                receipt_root=receipt,
                base_url="https://example.test",
                production_origin="https://example.test",
                mode="dry-run",
                attempt_id="retained-dry-run",
                retain_dry_run_store=retained_store,
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(instance.run(), 0)
            value = json.loads(output.getvalue())
            intent = value["proposed_intent"]
            retained = intent["retained_artifacts"]
            root = Path(retained["root"])
            verified = promote.verify_retained_dry_run_artifacts(
                root, expected_artifact_id=retained["artifact_id"]
            )
            self.assertEqual(verified.reference(), retained)
            self.assertEqual(intent["public_tree"]["root"], str(root / "public"))
            self.assertEqual(intent["public_result"]["public_dir"], str(root / "public"))
            self.assertEqual(
                intent["public_result"]["brain"]["destination"],
                str(root / "public/assets/brain"),
            )
            self.assertEqual(
                intent["public_result"]["brain"]["brain_page"]["destination"],
                str(root / "public/brain.html"),
            )
            self.assertEqual(intent["worker_bundle"]["tree"]["root"], str(root / "worker"))
            self.assertEqual(intent["worker_bundle"]["entry"], str(root / "worker/index.js"))
            self.assertEqual(intent["worker_bundle"]["config"], str(root / "wrangler.jsonc"))
            self.assertFalse((base / "workspace").exists())
            self.assertTrue(root.is_dir())
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o555)
            manifest = json.loads((root / "manifest.json").read_bytes())
            self.assertEqual(
                set(manifest),
                {
                    "schema",
                    "artifact_id",
                    "attempt_id",
                    "release_id",
                    "authority_git_commit",
                    "public_tree",
                    "worker_bundle",
                    "wrangler_config",
                    "evidence",
                },
            )
            self.assertEqual(manifest["schema"], promote.DRY_RUN_ARTIFACT_SCHEMA)
            self.assertEqual(manifest["public_tree"]["directories"], [])
            self.assertEqual(manifest["worker_bundle"]["directories"], [])
            self.assertEqual(
                set(manifest["evidence"]),
                {
                    "initial_selector",
                    "status_before",
                    "status_after",
                    "deployments_history",
                    "versions_history",
                },
            )
            self.assertEqual(
                (root / manifest["evidence"]["deployments_history"]["path"]).read_bytes(),
                prepared.history_raw["deployments"],
            )
            self.assertEqual(
                (root / manifest["evidence"]["versions_history"]["path"]).read_bytes(),
                prepared.history_raw["versions"],
            )
            for path in root.rglob("*"):
                self.assertFalse(path.is_symlink())
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode), 0o555 if path.is_dir() else 0o444
                )
                if path.is_file():
                    self.assertEqual(path.stat().st_nlink, 1)
            promote.remove_sealed_tree(root)

    def test_retention_rejects_overlapping_stores_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            forbidden = (
                repo / "retained",
                prepared.candidate.root / "retained",
                prepared.public_baseline.root / "retained",
                receipt / "retained",
                prepared.public_dir.parent / "retained",
            )
            for path in forbidden:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(promote.PromotionError, "outside"):
                        promote.retain_dry_run_artifacts(
                            prepared,
                            path,
                            repo_root=repo,
                            receipt_root=receipt,
                        )
                    self.assertFalse(path.exists())

    def test_retention_rejects_case_folded_protected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            alias = repo.with_name(repo.name.upper())
            if not alias.exists() or not os.path.samefile(alias, repo):
                self.skipTest("filesystem is case-sensitive")
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            with self.assertRaisesRegex(promote.PromotionError, "outside"):
                promote.retain_dry_run_artifacts(
                    prepared,
                    alias / "retained",
                    repo_root=repo,
                    receipt_root=receipt,
                )
            self.assertFalse((repo / "retained").exists())

    def test_retention_rejects_insecure_store_and_hardlinked_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            store = base / "retained"
            store.mkdir(mode=0o700)
            store.chmod(0o770)
            with self.assertRaisesRegex(promote.PromotionError, "private"):
                promote.retain_dry_run_artifacts(
                    prepared,
                    store,
                    repo_root=repo,
                    receipt_root=receipt,
                )

            store.chmod(0o700)
            outside_lock = base / "outside-lock"
            outside_lock.write_bytes(b"")
            os.link(outside_lock, store / ".retain.lock")
            with self.assertRaisesRegex(promote.PromotionError, "single-link"):
                promote.retain_dry_run_artifacts(
                    prepared,
                    store,
                    repo_root=repo,
                    receipt_root=receipt,
                )
            self.assertFalse(
                any(path.name.startswith(".pending-") for path in store.iterdir())
            )

    def test_retention_failure_removes_pending_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            store = base / "retained"
            with mock.patch.object(
                promote.shutil, "copytree", side_effect=OSError("injected copy failure")
            ):
                with self.assertRaisesRegex(OSError, "injected copy failure"):
                    promote.retain_dry_run_artifacts(
                        prepared,
                        store,
                        repo_root=repo,
                        receipt_root=receipt,
                    )
            self.assertTrue(store.is_dir())
            self.assertFalse(
                any(path.name.startswith(".pending-") for path in store.iterdir())
            )

    def test_retention_rejects_missing_or_inconsistent_raw_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            for broken in (
                replace(prepared, history_raw=None),
                replace(
                    prepared,
                    history_raw={"deployments": b"changed", "versions": b"[]\n"},
                ),
            ):
                with self.subTest(raw=broken.history_raw):
                    with self.assertRaisesRegex(promote.PromotionError, "history"):
                        promote.retain_dry_run_artifacts(
                            broken,
                            base / f"retained-{len(list(base.glob('retained-*')))}",
                            repo_root=repo,
                            receipt_root=receipt,
                        )

    def test_retained_artifact_verifier_rejects_tampering_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            retained = promote.retain_dry_run_artifacts(
                prepared,
                base / "retained",
                repo_root=repo,
                receipt_root=receipt,
            )
            retained.root.chmod(0o755)
            extra = retained.root / "extra"
            extra.write_bytes(b"extra")
            extra.chmod(0o444)
            retained.root.chmod(0o555)
            with self.assertRaisesRegex(promote.PromotionError, "top-level closure"):
                promote.verify_retained_dry_run_artifacts(retained.root)

            retained.root.chmod(0o755)
            extra.unlink()
            target = retained.root / "evidence/status-before.body"
            target.chmod(0o644)
            target.write_bytes(b"tampered")
            target.chmod(0o444)
            retained.root.chmod(0o555)
            with self.assertRaisesRegex(promote.PromotionError, "bytes differ"):
                promote.verify_retained_dry_run_artifacts(retained.root)
            promote.remove_sealed_tree(retained.root)

    def test_retained_artifact_verifier_rejects_extra_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            receipt = initialize_target_receipt_root(
                base / "receipts", repo, "https://example.test"
            )
            prepared = prepared_for_retention(base / "workspace")
            retained = promote.retain_dry_run_artifacts(
                prepared,
                base / "retained",
                repo_root=repo,
                receipt_root=receipt,
            )
            retained.public_dir.chmod(0o755)
            extra = retained.public_dir / "unexpected-empty-directory"
            extra.mkdir(mode=0o555)
            retained.public_dir.chmod(0o555)
            with self.assertRaisesRegex(promote.PromotionError, "tree differs"):
                promote.verify_retained_dry_run_artifacts(retained.root)
            promote.remove_sealed_tree(retained.root)

    def test_pending_cleanup_unlinks_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            outside = base / "outside"
            outside.mkdir(mode=0o755)
            outside.chmod(0o755)
            pending = base / ".pending-fixture"
            pending.symlink_to(outside, target_is_directory=True)
            promote._remove_pending_tree(pending)
            self.assertFalse(pending.exists())
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_retained_artifact_verifier_rejects_symlinks_and_hardlinks(self):
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                repo = make_repo(base)
                receipt = initialize_target_receipt_root(
                    base / "receipts", repo, "https://example.test"
                )
                prepared = prepared_for_retention(base / "workspace")
                retained = promote.retain_dry_run_artifacts(
                    prepared,
                    base / "retained",
                    repo_root=repo,
                    receipt_root=receipt,
                )
                target = retained.root / "evidence/status-before.body"
                retained.root.chmod(0o755)
                target.parent.chmod(0o755)
                target.unlink()
                outside = base / "outside-body"
                outside.write_bytes(b"outside")
                if link_kind == "symlink":
                    target.symlink_to(outside)
                else:
                    os.link(outside, target)
                    target.chmod(0o444)
                target.parent.chmod(0o555)
                retained.root.chmod(0o555)
                expected = "symlink" if link_kind == "symlink" else "hard-linked"
                with self.assertRaisesRegex(promote.PromotionError, expected):
                    promote.verify_retained_dry_run_artifacts(retained.root)
                promote.remove_sealed_tree(retained.root)

    def test_retain_option_is_dry_run_only_and_requires_absolute_store(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            repo = make_repo(base)
            with self.assertRaisesRegex(promote.PromotionError, "only with --dry-run"):
                promote.BrainPromoter(
                    repo_root=repo,
                    python=Path(sys.executable),
                    release_id=RELEASE_A,
                    release_root=base / "release" / ("a" * 64),
                    public_baseline_id=BASELINE_ID,
                    public_baseline_root=base / "baseline" / ("d" * 64),
                    receipt_root=base / "receipts",
                    base_url="https://example.test",
                    production_origin="https://example.test",
                    mode="execute",
                    retain_dry_run_store=base / "retained",
                )._validate_options()
            with self.assertRaisesRegex(promote.PromotionError, "absolute"):
                promote.BrainPromoter(
                    repo_root=repo,
                    python=Path(sys.executable),
                    release_id=RELEASE_A,
                    release_root=base / "release" / ("a" * 64),
                    public_baseline_id=BASELINE_ID,
                    public_baseline_root=base / "baseline" / ("d" * 64),
                    receipt_root=base / "receipts",
                    base_url="https://example.test",
                    production_origin="https://example.test",
                    mode="dry-run",
                    retain_dry_run_store=Path("relative-store"),
                )._validate_options()


if __name__ == "__main__":
    unittest.main()
