#!/usr/bin/env python3
from __future__ import annotations

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
        self.ops.mkdir(parents=True)
        self.home.mkdir()
        (self.mathlib / "Mathlib" / "Algebra").mkdir(parents=True)
        (self.repo / "wiki").mkdir()
        (self.repo / "wiki" / "package.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "wiki" / ".dev.vars").write_text(
            "PIPELINE_TOKEN=fixture-file-token\n", encoding="utf-8"
        )
        (self.repo / "site" / "moderate.py").write_text("# fixture\n", encoding="utf-8")

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
                self.assertNotIn("WIKILEAN_MATHLIB", environment)
            else:
                self.assertEqual(environment["WIKILEAN_MATHLIB"], str(self.mathlib))
                self.assertNotIn("BRAIN_MATHLIB_CHECKOUT", environment)

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
                    "export WIKILEAN_PYTHON WIKILEAN_MATHLIB BRAIN_MATHLIB_CHECKOUT",
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
            self.assertEqual(
                document["EnvironmentVariables"],
                {"WIKILEAN_PYTHON": str(Path(sys.executable).resolve())},
            )

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
