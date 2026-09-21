#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "brain-nightly.sh"
PROMOTER = HERE / "brain-promote-release.sh"


class BrainNightlyShellTest(unittest.TestCase):
    def test_disabled_run_derives_repo_root_from_script_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            ops = root / "site" / "ops"
            ops.mkdir(parents=True)
            copied_script = ops / "brain-nightly.sh"
            shutil.copyfile(SCRIPT, copied_script)
            for relative in (
                "brain/authority/reducer-inputs-v1.json",
                "site/build_brain_page.py",
                "wiki/package.json",
                "wiki/wrangler.jsonc",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            env = dict(os.environ)
            env["WIKILEAN_BRAIN_REFRESH"] = "0"
            result = subprocess.run(
                ["bash", str(copied_script)],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            skips = (ops / "logs" / "skips.log").read_text(encoding="utf-8")
            self.assertIn("brain refresh disabled", skips)

    def test_script_has_valid_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_promoter_wrapper_has_valid_bash_syntax_and_no_checkout_path(self):
        result = subprocess.run(["bash", "-n", str(PROMOTER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = PROMOTER.read_text(encoding="utf-8")
        self.assertNotIn("/Users/jack", text)
        self.assertIn("brain_promote_release.py", text)
        self.assertIn('exec "$PYTHON_BIN"', text)
        self.assertLess(text.index('  "$@" \\'), text.index('  --repo-root "$REPO"'))

    def test_missing_mathlib_fails_before_ingest_and_clears_prior_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            ops = root / "site" / "ops"
            ops.mkdir(parents=True)
            copied_script = ops / "brain-nightly.sh"
            shutil.copyfile(SCRIPT, copied_script)
            shutil.copyfile(HERE / "retry-lib.sh", ops / "retry-lib.sh")
            for relative in (
                "brain/authority/reducer-inputs-v1.json",
                "site/build_brain_page.py",
                "wiki/package.json",
                "wiki/wrangler.jsonc",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            out = root / "site" / "out"
            out.mkdir(parents=True)
            result_names = (
                "brain-release-result.json",
                "brain-release-metrics.json",
                "brain-public-result.json",
            )
            for name in result_names:
                (out / name).write_text('{"ok":true}\n', encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "WIKILEAN_BRAIN_REFRESH": "1",
                "WIKILEAN_PYTHON": sys.executable,
                "BRAIN_MATHLIB_CHECKOUT": "",
            })
            result = subprocess.run(
                ["bash", str(copied_script)],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(any((out / name).exists() for name in result_names))
            logs = list((ops / "logs").glob("brain-*.log"))
            self.assertEqual(len(logs), 1)
            log = logs[0].read_text(encoding="utf-8")
            self.assertIn("BRAIN_MATHLIB_CHECKOUT", log)
            self.assertNotIn("=== ingest", log)

    def test_launchd_invokes_this_checkout(self):
        self.assertFalse((HERE / "org.wikilean.brain.plist").exists())
        renderer = (HERE / "nightly-launchd.py").read_text(encoding="utf-8")
        self.assertIn('label="org.wikilean.brain"', renderer)
        self.assertIn('script="brain-nightly.sh"', renderer)

    def test_script_has_no_checkout_specific_absolute_path(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("/Users/jack", text)
        self.assertIn('SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"', text)

    def test_release_cleanliness_gate_covers_full_worker_and_release_sources(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('git -C "$REPO" status --porcelain -- \\\n      wiki/ site/assets', text)
        for path in (
            "brain/tools/build_release.py",
            "brain/tools/verify_release.py",
            "brain/tools/measure_store.py",
            "brain/tools/authority_contracts.py",
            "brain/authority/schemas",
            "site/ops/brain-canary.py",
            "site/ops/nightly.env",
        ):
            self.assertIn(path, text)
        self.assertIn('value.get("scope")', text)
        self.assertIn('$REDUCER_SCOPE', text)
        inventory = (HERE.parent.parent / "brain" / "authority" / "reducer-inputs-v1.json")
        scope = __import__("json").loads(inventory.read_text(encoding="utf-8"))["scope"]
        self.assertIn("brain/store.py", scope)
        self.assertIn("brain/frontier_suitability.py", scope)
        self.assertIn(
            'cmp -s "$RELEASE_ROOT/site/out/brain.html" "$REPO/wiki/public/brain.html"',
            text,
        )
        self.assertIn('--input-inventory "brain/authority/reducer-inputs-v1.json"', text)
        self.assertNotIn('--input-inventory "$REPO/', text)

    def test_release_metrics_and_public_stage_results_are_release_qualified(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('RELEASE_METRICS_RESULT="$REPO/site/out/brain-release-metrics.json"', text)
        self.assertIn('PUBLIC_RESULT="$REPO/site/out/brain-public-result.json"', text)
        self.assertIn('"$PYTHON_BIN" "$REPO/brain/tools/measure_store.py"', text)
        self.assertIn('STORE_METRICS_RELEASE_ID="$(store_metrics_release_id', text)
        self.assertIn('PUBLIC_STAGE_RELEASE_ID="$(public_result_release_id', text)
        self.assertIn(
            "schema public_dir mathlib_declarations public_baseline brain duration_ms max_rss_bytes",
            text,
        )
        self.assertIn('[ "$PUBLIC_STAGE_RELEASE_ID" = "$RELEASE_ID" ]', text)
        self.assertIn('"duration_ms", "max_rss_bytes", "free_bytes_before", "free_bytes_after"', text)

    def test_deployment_remains_disabled_by_default(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('BRAIN_DEPLOY="${WIKILEAN_BRAIN_DEPLOY:-0}"', text)
        self.assertIn('if [ "$BRAIN_DEPLOY" != "0" ]; then', text)
        self.assertIn("WIKILEAN_BRAIN_DEPLOY is retired", text)
        self.assertIn("brain-promote-release.sh", text)
        self.assertNotIn("npm run deploy", text)
        self.assertNotIn("wrangler rollback", text)
        self.assertNotIn("automatic rollback", text.lower())

    def test_legacy_deploy_flag_fails_before_mathlib_or_ingest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            ops = root / "site" / "ops"
            ops.mkdir(parents=True)
            copied_script = ops / "brain-nightly.sh"
            shutil.copyfile(SCRIPT, copied_script)
            shutil.copyfile(HERE / "retry-lib.sh", ops / "retry-lib.sh")
            for relative in (
                "brain/authority/reducer-inputs-v1.json",
                "site/build_brain_page.py",
                "wiki/package.json",
                "wiki/wrangler.jsonc",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "WIKILEAN_BRAIN_REFRESH": "1",
                "WIKILEAN_BRAIN_DEPLOY": "1",
                "WIKILEAN_PYTHON": sys.executable,
                "BRAIN_MATHLIB_CHECKOUT": "",
            })
            result = subprocess.run(
                ["bash", str(copied_script)],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            logs = list((ops / "logs").glob("brain-*.log"))
            self.assertEqual(len(logs), 1)
            log = logs[0].read_text(encoding="utf-8")
            self.assertIn("WIKILEAN_BRAIN_DEPLOY is retired", log)
            self.assertNotIn("BRAIN_MATHLIB_CHECKOUT must name", log)
            self.assertNotIn("=== ingest", log)

    def test_runtime_and_inputs_fail_closed_before_build(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('site/ops/nightly.local.env', text)
        self.assertIn("sys.version_info < (3, 12)", text)
        self.assertIn('BRAIN_MATHLIB_CHECKOUT="${BRAIN_MATHLIB_CHECKOUT:-}"', text)
        self.assertIn('[ ! -d "$BRAIN_MATHLIB_CHECKOUT/Algebra" ]', text)
        self.assertIn('AGENT_PY="${WIKILEAN_BRAIN_AGENT_PYTHON:-', text)
        self.assertIn('agent team skipped: SDK Python is missing', text)
        self.assertLess(
            text.index('BRAIN_MATHLIB_CHECKOUT must name a readable'),
            text.index('=== ingest: daily sources ==='),
        )

    def test_results_are_cleared_before_a_new_release_attempt(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'rm -f "$RELEASE_RESULT" "$RELEASE_METRICS_RESULT" "$PUBLIC_RESULT"',
            text,
        )
        self.assertLess(
            text.index('rm -f "$RELEASE_RESULT"'),
            text.index("sys.version_info < (3, 12)"),
        )

    def test_mutating_wrangler_commands_live_only_in_the_promoter(self):
        nightly = SCRIPT.read_text(encoding="utf-8")
        promoter = (HERE / "brain_promote_release.py").read_text(encoding="utf-8")
        self.assertNotIn('"wrangler", "rollback"', nightly)
        self.assertNotIn("npm run deploy", nightly)
        self.assertIn('"wrangler",\n            "deploy"', promoter)
        self.assertIn('"--no-bundle"', promoter)
        self.assertIn('"deploy_invocation"', promoter)


if __name__ == "__main__":
    unittest.main()
