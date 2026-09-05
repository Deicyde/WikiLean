#!/usr/bin/env python3
from __future__ import annotations

import json
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


def write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def make_gate_checkout(directory: str) -> tuple[Path, Path, Path, Path]:
    root = Path(directory) / "checkout"
    ops = root / "site" / "ops"
    ops.mkdir(parents=True)
    shutil.copyfile(SCRIPT, ops / "brain-nightly.sh")
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

    mathlib = root / "mathlib" / "Mathlib"
    (mathlib / "Algebra").mkdir(parents=True)
    logdir = ops / "logs"
    logdir.mkdir()
    (logdir / ".stamp.brain-weekly").touch()
    (logdir / ".stamp.brain-monthly").touch()
    command_log = root / "commands.jsonl"

    write_executable(root / "brain" / "fold_proposals.py", """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["TEST_COMMAND_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"command": "fold", "args": sys.argv[1:]}) + "\\n")
if "--write-wikidata-request-plan" in sys.argv:
    output = Path(sys.argv[sys.argv.index("--write-wikidata-request-plan") + 1])
    output.write_bytes(os.environ["TEST_PLAN_BYTES"].encode("utf-8"))
    output.chmod(0o644)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "command": "plan-mode",
            "parent_mode": oct(output.parent.stat().st_mode & 0o777),
        }) + "\\n")
    raise SystemExit(int(os.environ.get("TEST_PLAN_EXIT", "0")))
raise SystemExit(int(os.environ.get("TEST_FOLD_EXIT", "0")))
""")
    write_executable(root / "brain" / "acquire-wikidata-entities.sh", """#!/bin/bash
set -eu
printf '{"command":"acquire","python":"%s","plan":"%s","store":"%s"}\\n' \
  "$WIKILEAN_PYTHON" "$1" "$3" >>"$TEST_COMMAND_LOG"
case "${TEST_ACQUIRE_MODE:-success}" in
  fail) exit 9 ;;
esac
store="$3"
mkdir -p "$store"
chmod 0700 "$store"
target="$store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
mkdir -p "$target"
chmod 0700 "$target"
case "${TEST_ACQUIRE_MODE:-success}" in
  multi) printf '%s\\n%s\\n' "$target" "$target" ;;
  escape)
    mkdir -p "$TEST_OUTSIDE_BUNDLE"
    chmod 0700 "$TEST_OUTSIDE_BUNDLE"
    printf '%s\\n' "$TEST_OUTSIDE_BUNDLE"
    ;;
  relative) printf '%s\\n' relative-bundle ;;
  *) printf '%s\\n' "$target" ;;
esac
""")
    write_executable(root / "brain" / "build_snapshot.py", """#!/usr/bin/env python3
import json
import os
from pathlib import Path
with Path(os.environ["TEST_COMMAND_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"command": "build"}) + "\\n")
raise SystemExit(1)
""")
    return (
        root,
        mathlib,
        command_log,
        root / "catalog" / ".cache" / "wikidata" / "entity-bundles",
    )


def run_gate_checkout(
    root: Path,
    mathlib: Path,
    command_log: Path,
    store: Path,
    *,
    plan_bytes: str,
    acquire_mode: str = "success",
    plan_exit: int = 0,
    fold_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "WIKILEAN_BRAIN_REFRESH": "1",
        "WIKILEAN_BRAIN_AGENTS": "0",
        "WIKILEAN_BRAIN_DEPLOY": "0",
        "WIKILEAN_PYTHON": sys.executable,
        "BRAIN_MATHLIB_CHECKOUT": str(mathlib),
        "TEST_COMMAND_LOG": str(command_log),
        "TEST_PLAN_BYTES": plan_bytes,
        "TEST_ACQUIRE_MODE": acquire_mode,
        "TEST_PLAN_EXIT": str(plan_exit),
        "TEST_FOLD_EXIT": str(fold_exit),
        "TEST_OUTSIDE_BUNDLE": str(root / "outside-bundle"),
    })
    return subprocess.run(
        ["bash", str(root / "site" / "ops" / "brain-nightly.sh")],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
    )


def command_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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

    def test_lock_is_never_stolen_by_age(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("-mmin +240", text)
        self.assertIn("refusing age-only recovery", text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            ops = root / "site" / "ops"
            ops.mkdir(parents=True)
            shutil.copyfile(SCRIPT, ops / "brain-nightly.sh")
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
            lock = ops / "logs" / ".lock.brain.d"
            lock.mkdir(parents=True)
            os.utime(lock, (1, 1))
            env = dict(os.environ)
            env["WIKILEAN_BRAIN_REFRESH"] = "1"
            result = subprocess.run(
                ["bash", str(ops / "brain-nightly.sh")],
                cwd="/",
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(lock.is_dir())
            skips = (ops / "logs" / "skips.log").read_text(encoding="utf-8")
            self.assertIn("refusing age-only recovery", skips)

    def test_wikidata_gate_structure_is_private_exact_and_fail_closed(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count("trap cleanup EXIT"), 1)
        self.assertNotIn("rm -rf", text)
        self.assertIn('RUN_DIR="$(mktemp -d "$LOGDIR/.brain-run.XXXXXX")"', text)
        self.assertIn('chmod 0700 "$RUN_DIR"', text)
        self.assertIn('FOLD_ARGS=()', text)
        self.assertIn("wikilean.wikidata-entity-request-plan/v1", text)
        self.assertIn('plan["qids"]', text)
        self.assertIn('data != canonical', text)
        self.assertIn('WIKILEAN_PYTHON="$PYTHON_BIN"', text)
        self.assertIn('"$REPO/brain/acquire-wikidata-entities.sh"', text)
        self.assertIn('--store "$WIKIDATA_ENTITY_STORE"', text)
        self.assertIn('"${FOLD_ARGS[@]}"', text)
        self.assertIn("acquirer must print exactly one newline-terminated path", text)
        self.assertIn("acquirer output escaped the configured store", text)

    def test_empty_wikidata_plan_skips_acquisition_and_folds_without_args(self):
        with tempfile.TemporaryDirectory() as directory:
            root, mathlib, commands, store = make_gate_checkout(directory)
            plan = json.dumps(
                {"schema": "wikilean.wikidata-entity-request-plan/v1", "qids": []},
                sort_keys=True,
                separators=(",", ":"),
            )
            result = run_gate_checkout(
                root, mathlib, commands, store, plan_bytes=plan
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            records = command_records(commands)
            self.assertEqual([row["command"] for row in records], [
                "fold", "plan-mode", "fold", "build",
            ])
            self.assertEqual(records[0]["args"][0], "--write-wikidata-request-plan")
            self.assertEqual(records[1]["parent_mode"], "0o700")
            self.assertEqual(records[2]["args"], [])
            self.assertFalse(store.exists())
            self.assertFalse(list((root / "site" / "ops" / "logs").glob(".brain-run.*")))
            log = next((root / "site" / "ops" / "logs").glob("brain-*.log"))
            self.assertIn("Wikidata request set empty — acquisition skipped", log.read_text())

    def test_nonempty_wikidata_plan_acquires_one_confined_bundle_then_folds(self):
        with tempfile.TemporaryDirectory() as directory:
            root, mathlib, commands, store = make_gate_checkout(directory)
            plan = json.dumps(
                {
                    "schema": "wikilean.wikidata-entity-request-plan/v1",
                    "qids": ["Q1", "Q2"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            result = run_gate_checkout(
                root, mathlib, commands, store, plan_bytes=plan
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            records = command_records(commands)
            self.assertEqual([row["command"] for row in records], [
                "fold", "plan-mode", "acquire", "fold", "build",
            ])
            acquire = records[2]
            self.assertEqual(acquire["python"], sys.executable)
            self.assertEqual(acquire["store"], str(store.resolve()))
            self.assertEqual(acquire["plan"], records[0]["args"][1])
            expected_bundle = (store / ("a" * 64)).resolve()
            self.assertEqual(
                records[3]["args"],
                ["--wikidata-entity-bundle", str(expected_bundle)],
            )
            self.assertEqual(store.stat().st_mode & 0o777, 0o700)
            self.assertEqual(expected_bundle.stat().st_mode & 0o777, 0o700)
            self.assertFalse(list((root / "site" / "ops" / "logs").glob(".brain-run.*")))

    def test_wikidata_plan_and_acquisition_failures_never_reach_fold_or_build(self):
        invalid_plans = (
            '{"qids":["Q1"],"schema":"wrong"}',
            '{"extra":true,"qids":["Q1"],"schema":"wikilean.wikidata-entity-request-plan/v1"}',
            '{"qids":["Q1","Q1"],"schema":"wikilean.wikidata-entity-request-plan/v1"}',
            '{ "qids": ["Q1"], "schema": "wikilean.wikidata-entity-request-plan/v1" }',
        )
        for plan in invalid_plans:
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as directory:
                root, mathlib, commands, store = make_gate_checkout(directory)
                result = run_gate_checkout(
                    root, mathlib, commands, store, plan_bytes=plan
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                names = [row["command"] for row in command_records(commands)]
                self.assertEqual(names, ["fold", "plan-mode"])

        valid_plan = json.dumps(
            {"schema": "wikilean.wikidata-entity-request-plan/v1", "qids": ["Q1"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        for mode in ("fail", "multi", "escape", "relative"):
            with self.subTest(acquire_mode=mode), tempfile.TemporaryDirectory() as directory:
                root, mathlib, commands, store = make_gate_checkout(directory)
                result = run_gate_checkout(
                    root,
                    mathlib,
                    commands,
                    store,
                    plan_bytes=valid_plan,
                    acquire_mode=mode,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                names = [row["command"] for row in command_records(commands)]
                self.assertEqual(names, ["fold", "plan-mode", "acquire"])

        with tempfile.TemporaryDirectory() as directory:
            root, mathlib, commands, store = make_gate_checkout(directory)
            result = run_gate_checkout(
                root,
                mathlib,
                commands,
                store,
                plan_bytes=valid_plan,
                fold_exit=7,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            names = [row["command"] for row in command_records(commands)]
            self.assertEqual(names, ["fold", "plan-mode", "acquire", "fold"])

    def test_release_cleanliness_gate_covers_full_worker_and_release_sources(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('git -C "$REPO" status --porcelain -- \\\n      wiki/ site/assets', text)
        for path in (
            "brain/acquire-wikidata-entities.sh",
            "brain/acquire_wikidata_entities.py",
            "brain/wikidata_entity_bundle.py",
            "brain/fold_proposals.py",
            "brain/ingest/common.py",
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
