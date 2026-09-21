#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent


class NightlyLaunchdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.repo = base / "checkout & portable@example"
        self.ops = self.repo / "site" / "ops"
        self.home = base / "home & local"
        self.mathlib = base / "mathlib4"
        self.observation_plan = base / "reviewed observation plan.json"
        self.ops.mkdir(parents=True)
        self.home.mkdir()
        (self.mathlib / "Mathlib" / "Algebra").mkdir(parents=True)
        self.observation_plan.write_text("{}\n", encoding="utf-8")
        self.observation_plan_sha256 = hashlib.sha256(
            self.observation_plan.read_bytes()
        ).hexdigest()
        (self.repo / "wiki").mkdir()
        (self.repo / "wiki" / "package.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "wiki" / "wrangler.jsonc").write_text("{}\n", encoding="utf-8")
        (self.repo / "wiki" / ".dev.vars").write_text(
            "PIPELINE_TOKEN=fixture-file-token\n", encoding="utf-8"
        )
        (self.repo / "site" / "moderate.py").write_text("# fixture\n", encoding="utf-8")
        (self.repo / "site" / "build_brain_page.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        authority = self.repo / "brain" / "authority"
        authority.mkdir(parents=True)
        (authority / "reducer-inputs-v1.json").write_text("{}\n", encoding="utf-8")

        for name in (
            "launchd-plist.template",
            "nightly-launchd.py",
            "nightly-runtime.sh",
            "brain-nightly.sh",
            "nightly-moderate.sh",
            "newtags-nightly.sh",
            "new-once.sh",
            "run-now.sh",
            "retry-lib.sh",
            "nightly.env",
            "nightly.local.env.example",
        ):
            shutil.copy2(HERE / name, self.ops / name)

    def environment(self, *, preflight: bool = False) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "WIKILEAN_API_TOKEN": "fixture-token",
            "WIKILEAN_MATHLIB": str(self.mathlib),
            "WIKILEAN_PYTHON": sys.executable,
        }
        if preflight:
            env["WIKILEAN_OPS_PREFLIGHT_ONLY"] = "1"
        return env

    def run_command(
        self,
        *command: str,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd or self.repo,
            env=env or self.environment(),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def launchd_arguments(self) -> list[str]:
        return [
            "--python",
            str(Path(sys.executable).resolve()),
            "--mathlib",
            str(self.mathlib),
            "--wikidata-observation-plan",
            str(self.observation_plan),
            "--wikidata-observation-plan-sha256",
            self.observation_plan_sha256,
        ]

    def moderation_environment(self, fake_python: Path) -> dict[str, str]:
        env = self.environment()
        env.update(
            {
                "WIKILEAN_PYTHON": str(fake_python),
                "WIKILEAN_WD_EMBED_REFRESH": "0",
                "WIKILEAN_FORMALIZE_LIMIT": "0",
                "WIKILEAN_AUTO_DECIDE": "0",
                "WIKILEAN_GRAPH_REFRESH": "0",
                "WIKILEAN_GRAPH_DEPLOY": "0",
                "WIKILEAN_DECLCITES_REFRESH": "0",
                "WIKILEAN_LIBDECLS_REFRESH": "0",
                "WIKILEAN_GOLDEN_REFRESH": "0",
                "WIKILEAN_COMMUNITY_HARVEST": "1",
            }
        )
        return env

    def fake_python(self) -> tuple[Path, Path]:
        executable = Path(self.temporary.name).resolve() / "fake-python3"
        log = Path(self.temporary.name).resolve() / "fake-python.log"
        executable.write_text(
            """#!/bin/bash
if [ "${1:-}" = "-c" ]; then
  exit 0
fi
for argument in "$@"; do
  printf '%s\\n' "$argument" >>"$FAKE_PY_LOG"
done
printf '%s\\n' -- >>"$FAKE_PY_LOG"
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable, log

    def test_every_launcher_discovers_a_copied_checkout(self) -> None:
        for name in (
            "nightly-moderate.sh",
            "newtags-nightly.sh",
            "new-once.sh",
            "run-now.sh",
        ):
            with self.subTest(name=name):
                result = self.run_command(
                    "/bin/bash",
                    str(self.ops / name),
                    env=self.environment(preflight=True),
                    cwd="/",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"repo={self.repo}", result.stdout)
                self.assertIn(f"python={sys.executable}", result.stdout)
                self.assertIn(f"mathlib={self.mathlib}", result.stdout)

    def test_auto_selection_skips_non_312_python(self) -> None:
        rejected = self.repo / ".venv" / "bin" / "python3"
        rejected.parent.mkdir(parents=True)
        rejected.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        rejected.chmod(0o755)
        selected = self.repo / "catalog" / ".venv" / "bin" / "python3"
        selected.parent.mkdir(parents=True)
        selected.symlink_to(sys.executable)
        env = self.environment()
        env.pop("WIKILEAN_PYTHON")

        result = self.run_command(
            "/bin/bash",
            str(self.ops / "nightly-runtime.sh"),
            "check",
            env=env,
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"python={selected}", result.stdout)

    def test_local_env_supplies_host_paths(self) -> None:
        local_env = self.ops / "nightly.local.env"
        local_env.write_text(
            "\n".join(
                (
                    f"WIKILEAN_PYTHON={shlex.quote(sys.executable)}",
                    f"WIKILEAN_MATHLIB={shlex.quote(str(self.mathlib))}",
                    "export WIKILEAN_PYTHON WIKILEAN_MATHLIB",
                    "",
                )
            ),
            encoding="utf-8",
        )
        env = self.environment()
        env.pop("WIKILEAN_PYTHON")
        env.pop("WIKILEAN_MATHLIB")

        result = self.run_command(
            "/bin/bash", str(self.ops / "nightly-runtime.sh"), "check", env=env, cwd="/"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"python={sys.executable}", result.stdout)
        self.assertIn(f"mathlib={self.mathlib}", result.stdout)

    def test_missing_mathlib_config_fails_clearly(self) -> None:
        env = self.environment()
        env.pop("WIKILEAN_MATHLIB")
        result = self.run_command(
            "/bin/bash", str(self.ops / "nightly-runtime.sh"), "check", env=env
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WIKILEAN_MATHLIB is required", result.stderr)
        self.assertIn("nightly.local.env", result.stderr)

    def test_invalid_explicit_python_fails_clearly(self) -> None:
        invalid = self.repo / "python-too-old"
        invalid.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        invalid.chmod(0o755)
        env = self.environment()
        env["WIKILEAN_PYTHON"] = str(invalid)
        result = self.run_command(
            "/bin/bash", str(self.ops / "nightly-runtime.sh"), "check", env=env
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12+", result.stderr)
        self.assertIn(str(invalid), result.stderr)

    def test_missing_token_fails_without_printing_a_secret(self) -> None:
        env = self.environment()
        env.pop("WIKILEAN_API_TOKEN")
        (self.repo / "wiki" / ".dev.vars").unlink()
        result = self.run_command(
            "/bin/bash", str(self.ops / "nightly-runtime.sh"), "check", env=env
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPELINE_TOKEN", result.stderr)
        self.assertNotIn("fixture-token", result.stderr)

    def test_rendered_plists_are_absolute_and_valid(self) -> None:
        output = Path(self.temporary.name) / "rendered"
        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "render",
            "--output-dir",
            str(output),
            *self.launchd_arguments(),
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        expectations = {
            "org.wikilean.moderate": ("nightly-moderate.sh", 3, 20),
            "org.wikilean.newtags": ("newtags-nightly.sh", 3, 10),
            "org.wikilean.brain": ("brain-nightly.sh", 2, 20),
        }
        plutil = shutil.which("plutil")
        for label, (script, hour, minute) in expectations.items():
            path = output / f"{label}.plist"
            if plutil:
                lint = self.run_command(plutil, "-lint", str(path), cwd="/")
                self.assertEqual(lint.returncode, 0, lint.stderr)
            with path.open("rb") as stream:
                document = plistlib.load(stream)
            self.assertEqual(document["Label"], label)
            self.assertEqual(
                document["ProgramArguments"], ["/bin/bash", str(self.ops / script)]
            )
            self.assertEqual(document["StartCalendarInterval"], {"Hour": hour, "Minute": minute})
            self.assertTrue(Path(document["StandardOutPath"]).is_absolute())
            self.assertTrue(Path(document["StandardErrorPath"]).is_absolute())
            self.assertIsNone(re.search(r"@[A-Z]+@", path.read_text(encoding="utf-8")))
            environment = document["EnvironmentVariables"]
            self.assertEqual(environment["WIKILEAN_PYTHON"], str(Path(sys.executable).resolve()))
            if label == "org.wikilean.brain":
                self.assertEqual(
                    environment["BRAIN_MATHLIB_CHECKOUT"], str(self.mathlib / "Mathlib")
                )
                self.assertEqual(
                    environment["WIKILEAN_WIKIDATA_OBSERVATION_PLAN"],
                    str(self.observation_plan),
                )
                self.assertEqual(
                    environment["WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256"],
                    self.observation_plan_sha256,
                )
                self.assertNotIn("WIKILEAN_MATHLIB", environment)
            else:
                self.assertEqual(environment["WIKILEAN_MATHLIB"], str(self.mathlib))
                self.assertNotIn("BRAIN_MATHLIB_CHECKOUT", environment)
                self.assertNotIn("WIKILEAN_WIKIDATA_OBSERVATION_PLAN", environment)
                self.assertNotIn("WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256", environment)

    def test_check_and_install_never_load_a_job(self) -> None:
        check = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *self.launchd_arguments(),
            cwd="/",
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("launchd templates ok", check.stdout)

        destination = Path(self.temporary.name) / "LaunchAgents"
        install = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "install",
            "--output-dir",
            str(destination),
            *self.launchd_arguments(),
            cwd="/",
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn("no jobs were loaded or started", install.stdout)
        self.assertEqual(len(list(destination.glob("org.wikilean.*.plist"))), 3)

    def test_check_ignores_hostile_interactive_overrides(self) -> None:
        (self.ops / "nightly.local.env").write_text(
            "\n".join(
                (
                    f"WIKILEAN_PYTHON={shlex.quote(str(Path(sys.executable).resolve()))}",
                    f"WIKILEAN_MATHLIB={shlex.quote(str(self.mathlib))}",
                    f"BRAIN_MATHLIB_CHECKOUT={shlex.quote(str(self.mathlib / 'Mathlib'))}",
                    f"WIKILEAN_WIKIDATA_OBSERVATION_PLAN={shlex.quote(str(self.observation_plan))}",
                    f"WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256={self.observation_plan_sha256}",
                    "export WIKILEAN_PYTHON WIKILEAN_MATHLIB BRAIN_MATHLIB_CHECKOUT",
                    "export WIKILEAN_WIKIDATA_OBSERVATION_PLAN WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256",
                    "",
                )
            ),
            encoding="utf-8",
        )
        env = self.environment()
        env.update(
            {
                "WIKILEAN_PYTHON": "/hostile/python",
                "WIKILEAN_MATHLIB": "/hostile/mathlib",
                "BRAIN_MATHLIB_CHECKOUT": "/hostile/Mathlib",
                "WIKILEAN_WIKIDATA_OBSERVATION_PLAN": "/hostile/observation-plan.json",
                "WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256": "0" * 64,
                "WIKILEAN_API_TOKEN": "hostile-token",
            }
        )
        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            env=env,
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("hostile", result.stdout + result.stderr)
        self.assertIn(f"mathlib={self.mathlib}", result.stdout)

        output = Path(self.temporary.name) / "local-render"
        rendered = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "render",
            "--output-dir",
            str(output),
            env=env,
            cwd="/",
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        for path in output.glob("*.plist"):
            with path.open("rb") as stream:
                document = plistlib.load(stream)
            expected_environment = {
                "WIKILEAN_PYTHON": str(Path(sys.executable).resolve())
            }
            if document["Label"] == "org.wikilean.brain":
                expected_environment["WIKILEAN_WIKIDATA_OBSERVATION_PLAN"] = str(
                    self.observation_plan
                )
                expected_environment["WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256"] = (
                    self.observation_plan_sha256
                )
            self.assertEqual(document["EnvironmentVariables"], expected_environment)

    def test_explicit_observation_plan_override_wins_local_config_and_is_sealed(self) -> None:
        local_plan = self.repo / "local observation plan.json"
        local_plan.write_text("{}\n", encoding="utf-8")
        local_plan_sha256 = hashlib.sha256(local_plan.read_bytes()).hexdigest()
        (self.ops / "nightly.local.env").write_text(
            f"WIKILEAN_WIKIDATA_OBSERVATION_PLAN={shlex.quote(str(local_plan))}\n"
            f"WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256={local_plan_sha256}\n"
            "export WIKILEAN_WIKIDATA_OBSERVATION_PLAN "
            "WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256\n",
            encoding="utf-8",
        )
        output = Path(self.temporary.name) / "explicit-plan-render"
        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "render",
            "--job",
            "brain",
            "--output-dir",
            str(output),
            "--python",
            str(Path(sys.executable).resolve()),
            "--brain-mathlib",
            str(self.mathlib / "Mathlib"),
            "--wikidata-observation-plan",
            str(self.observation_plan),
            "--wikidata-observation-plan-sha256",
            self.observation_plan_sha256,
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with (output / "org.wikilean.brain.plist").open("rb") as stream:
            document = plistlib.load(stream)
        self.assertEqual(
            document["EnvironmentVariables"]["WIKILEAN_WIKIDATA_OBSERVATION_PLAN"],
            str(self.observation_plan),
        )
        self.assertEqual(
            document["EnvironmentVariables"]["WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256"],
            self.observation_plan_sha256,
        )

    def test_brain_preflight_rejects_invalid_observation_plan_binding(self) -> None:
        common = (
            "--job",
            "brain",
            "--python",
            str(Path(sys.executable).resolve()),
            "--brain-mathlib",
            str(self.mathlib / "Mathlib"),
        )
        missing = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *common,
            cwd="/",
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("WIKILEAN_WIKIDATA_OBSERVATION_PLAN is required", missing.stderr)

        for unpaired in (
            ("--wikidata-observation-plan", str(self.observation_plan)),
            ("--wikidata-observation-plan-sha256", self.observation_plan_sha256),
        ):
            with self.subTest(unpaired=unpaired[0]):
                result = self.run_command(
                    sys.executable,
                    str(self.ops / "nightly-launchd.py"),
                    "check",
                    *common,
                    *unpaired,
                    cwd="/",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be supplied together", result.stderr)

        relative = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *common,
            "--wikidata-observation-plan",
            "relative/plan.json",
            "--wikidata-observation-plan-sha256",
            self.observation_plan_sha256,
            cwd="/",
        )
        self.assertNotEqual(relative.returncode, 0)
        self.assertIn("--wikidata-observation-plan must be an absolute path", relative.stderr)

        bad_digest = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *common,
            "--wikidata-observation-plan",
            str(self.observation_plan),
            "--wikidata-observation-plan-sha256",
            "A" * 64,
            cwd="/",
        )
        self.assertNotEqual(bad_digest.returncode, 0)
        self.assertIn("must be 64 lowercase hexadecimal characters", bad_digest.stderr)

        mismatch = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *common,
            "--wikidata-observation-plan",
            str(self.observation_plan),
            "--wikidata-observation-plan-sha256",
            "0" * 64,
            cwd="/",
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("SHA-256 mismatch", mismatch.stderr)

        non_file = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *common,
            "--wikidata-observation-plan",
            str(self.home),
            "--wikidata-observation-plan-sha256",
            self.observation_plan_sha256,
            cwd="/",
        )
        self.assertNotEqual(non_file.returncode, 0)
        self.assertIn("must name a readable regular file", non_file.stderr)

    def test_brain_plist_digest_rejects_plan_replaced_after_render(self) -> None:
        output = Path(self.temporary.name) / "replacement-render"
        rendered = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "render",
            "--job",
            "brain",
            "--output-dir",
            str(output),
            *self.launchd_arguments(),
            cwd="/",
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        with (output / "org.wikilean.brain.plist").open("rb") as stream:
            document = plistlib.load(stream)

        self.observation_plan.write_text('{"replacement":true}\n', encoding="utf-8")
        launch_environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            **document["EnvironmentVariables"],
        }
        launched = self.run_command(
            "/bin/bash",
            str(self.ops / "brain-nightly.sh"),
            env=launch_environment,
            cwd="/",
        )
        self.assertNotEqual(launched.returncode, 0)
        logs = list((self.ops / "logs").glob("brain-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("SHA-256 mismatch", logs[0].read_text(encoding="utf-8"))

    def test_launchd_check_does_not_inherit_interactive_token(self) -> None:
        (self.repo / "wiki" / ".dev.vars").unlink()
        env = self.environment()
        env["WIKILEAN_API_TOKEN"] = "interactive-token-must-not-pass"
        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            *self.launchd_arguments(),
            env=env,
            cwd="/",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPELINE_TOKEN", result.stderr)
        self.assertNotIn("interactive-token", result.stdout + result.stderr)

    def test_relative_runtime_and_cli_paths_fail_from_root(self) -> None:
        for key, value, expected in (
            ("WIKILEAN_PYTHON", "relative/python3", "WIKILEAN_PYTHON must be an absolute"),
            ("WIKILEAN_MATHLIB", "relative/mathlib4", "WIKILEAN_MATHLIB must be absolute"),
        ):
            with self.subTest(key=key):
                env = self.environment()
                env[key] = value
                result = self.run_command(
                    "/bin/bash",
                    str(self.ops / "nightly-runtime.sh"),
                    "check",
                    env=env,
                    cwd="/",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            "--python",
            "relative/python3",
            "--mathlib",
            str(self.mathlib),
            cwd="/",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--python must be an absolute path", result.stderr)

        env = self.environment()
        env["BRAIN_MATHLIB_CHECKOUT"] = "relative/Mathlib"
        result = self.run_command(
            "/bin/bash",
            str(self.ops / "nightly-runtime.sh"),
            "check",
            "brain",
            env=env,
            cwd="/",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BRAIN_MATHLIB_CHECKOUT must be absolute", result.stderr)

        result = self.run_command(
            sys.executable,
            str(self.ops / "nightly-launchd.py"),
            "check",
            "--job",
            "brain",
            "--python",
            str(Path(sys.executable).resolve()),
            "--brain-mathlib",
            "relative/Mathlib",
            cwd="/",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--brain-mathlib must be an absolute path", result.stderr)

    def test_community_harvest_missing_bundle_fails_closed(self) -> None:
        fake_python, fake_log = self.fake_python()
        output = self.repo / "brain" / "data" / "community_edges.jsonl"
        output.parent.mkdir(parents=True)
        output.write_text("sentinel\n", encoding="utf-8")
        env = self.moderation_environment(fake_python)
        env["FAKE_PY_LOG"] = str(fake_log)
        env["WIKILEAN_D1_SNAPSHOT_BUNDLE"] = "relative/bundle"

        result = self.run_command(
            "/bin/bash",
            str(self.ops / "nightly-moderate.sh"),
            env=env,
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = list((self.repo / "site" / "cache" / "cron").glob("moderate-*.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("requires an absolute existing WIKILEAN_D1_SNAPSHOT_BUNDLE", text)
        self.assertIn("keeping the prior community_edges.jsonl", text)
        self.assertNotIn("harvest_community_edges.py", fake_log.read_text(encoding="utf-8"))
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")

    def test_community_harvest_passes_explicit_bundle(self) -> None:
        fake_python, fake_log = self.fake_python()
        bundle = Path(self.temporary.name).resolve() / "sealed bundle"
        bundle.mkdir()
        env = self.moderation_environment(fake_python)
        env["FAKE_PY_LOG"] = str(fake_log)
        env["WIKILEAN_D1_SNAPSHOT_BUNDLE"] = str(bundle)

        result = self.run_command(
            "/bin/bash",
            str(self.ops / "nightly-moderate.sh"),
            env=env,
            cwd="/",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = fake_log.read_text(encoding="utf-8").splitlines()
        harvester = str(self.repo / "brain" / "harvest_community_edges.py")
        index = arguments.index(harvester)
        self.assertEqual(arguments[index : index + 3], [harvester, "--snapshot-bundle", str(bundle)])

    def test_community_harvest_is_disabled_by_default(self) -> None:
        defaults = (self.ops / "nightly.env").read_text(encoding="utf-8")
        wrapper = (self.ops / "nightly-moderate.sh").read_text(encoding="utf-8")
        self.assertIn('${WIKILEAN_COMMUNITY_HARVEST:=0}', defaults)
        self.assertIn('--snapshot-bundle "$COMMUNITY_BUNDLE"', wrapper)

    def test_portable_sources_contain_no_users_checkout(self) -> None:
        names = (
            "launchd-plist.template",
            "nightly-launchd.py",
            "nightly-runtime.sh",
            "brain-nightly.sh",
            "nightly-moderate.sh",
            "newtags-nightly.sh",
            "new-once.sh",
            "run-now.sh",
            "nightly.env",
            "nightly.local.env.example",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertNotIn("/Users/", (self.ops / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
