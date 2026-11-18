#!/usr/bin/env python3
"""The locate unit's two opt-ins and its wiring, and the read-only commands."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes
from wikilean import locate, model, secrets
from wikilean.cli import main
from wikilean.config import Config
from wikilean.work import EX_FAILED, EX_NOTHING, EX_OK, Options

TOKEN = "wla_" + "E" * 43


class LocateGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, fakes.private_home(self.directory), clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(secrets.ENV_TOKEN, None)
        # On macOS the token lookup asks the login Keychain through `security`, a subprocess the
        # dry-run test forbids; these tests read the token file only, as on Linux.
        keychain = mock.patch.object(secrets, "_security", return_value=None)
        keychain.start()
        self.addCleanup(keychain.stop)

    def ready_config(self) -> Config:
        """A config in which every precondition but the token is satisfied."""
        checkout = self.directory / "WikiLean"
        for script in (locate.RUN_PACKAGE, locate.SUBMIT):
            path = checkout / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# stand-in\n", encoding="utf-8")
        repo = self.directory / "work-repo"
        repo.mkdir(exist_ok=True)
        return Config(locate_enabled=True, locate_checkout=str(checkout),
                      locate_repo=str(repo), locate_workdir=str(self.directory / "wd"))

    def test_the_config_flag_alone_does_not_start_it(self):
        outcome = locate.run_locate(self.ready_config(), Options(enable_locate=False), log=lambda *_: None)
        self.assertEqual(outcome.code, EX_NOTHING)
        self.assertIn("--enable-locate", outcome.summary)

    def test_the_command_flag_alone_does_not_start_it(self):
        config = self.ready_config()
        config.locate_enabled = False
        outcome = locate.run_locate(config, Options(enable_locate=True), log=lambda *_: None)
        self.assertEqual(outcome.code, EX_NOTHING)
        self.assertIn("locate_enabled", outcome.summary)

    def test_with_both_opt_ins_it_still_refuses_until_the_prerequisites_exist(self):
        config = Config(locate_enabled=True)
        gaps = locate.preconditions(config)
        self.assertTrue(any("checkout" in gap.what for gap in gaps))
        self.assertTrue(any("work repository" in gap.what for gap in gaps))
        self.assertTrue(any("agent token" in gap.what for gap in gaps))
        outcome = locate.run_locate(config, Options(enable_locate=True), log=lambda *_: None)
        self.assertEqual(outcome.code, EX_FAILED)

    def test_a_ready_configuration_reports_only_the_missing_token(self):
        gaps = locate.preconditions(self.ready_config())
        self.assertEqual([gap.what for gap in gaps], ["an agent token"])

    def test_the_dry_run_prints_the_sequence_and_runs_nothing(self):
        secrets.store_token(TOKEN, prefer="file")
        printed: list[str] = []
        with mock.patch.object(locate.subprocess, "run", side_effect=AssertionError("ran a command")):
            outcome = locate.run_locate(self.ready_config(), Options(enable_locate=True, dry_run=True),
                                        log=printed.append)
        self.assertEqual(outcome.code, EX_OK)
        text = "\n".join(printed)
        self.assertIn("run_package.py", text)
        self.assertIn("--stage claim", text.replace("--stage claim --execute", "--stage claim"))
        self.assertIn("submit.py", text)
        self.assertNotIn(TOKEN, text)

    def test_the_free_stages_come_before_the_paid_one(self):
        names = [name for name, _ in locate.steps(self.ready_config())]
        argvs = [" ".join(argv) for _, argv in locate.steps(self.ready_config())]
        paid = next(i for i, argv in enumerate(argvs) if "--stage run --execute" in argv)
        preview = next(i for i, argv in enumerate(argvs) if argv.endswith("--stage run"))
        self.assertLess(preview, paid, names)
        submitted = next(i for i, argv in enumerate(argvs) if "submit.py" in argv and "--execute" in argv)
        self.assertLess(paid, submitted)

    def test_only_the_submission_steps_are_given_the_token(self):
        for name, _argv in locate.steps(self.ready_config()):
            environment = locate.child_environment(name, TOKEN)
            if "submit" in name:
                self.assertEqual(environment[secrets.ENV_TOKEN], TOKEN)
            else:
                self.assertNotIn(secrets.ENV_TOKEN, environment)

    def test_the_token_is_never_an_argument_of_any_step(self):
        for _name, argv in locate.steps(self.ready_config()):
            self.assertNotIn(TOKEN, " ".join(argv))
            self.assertFalse(any(part.startswith("wla_") for part in argv))

    def test_an_inherited_token_is_stripped_from_the_unpaid_steps(self):
        with mock.patch.dict(os.environ, {secrets.ENV_TOKEN: TOKEN}):
            self.assertNotIn(secrets.ENV_TOKEN, locate.child_environment("the paid model run", None))

    def test_no_step_is_handed_an_anthropic_key(self):
        """CLAUDE.md's "Max-auth gotcha": with ANTHROPIC_API_KEY set, a
        subscription `claude` call fails silently ("error result: success", 0
        tokens). scripts/pipeline/contribute/bridge.sh unsets it before running
        exactly this pipeline; so does model.py's allowlist; so does this."""
        ambient = {"ANTHROPIC_API_KEY": "sk-should-not-travel",
                   "ANTHROPIC_BASE_URL": "https://elsewhere.invalid"}
        with mock.patch.dict(os.environ, ambient):
            for name, _argv in locate.steps(self.ready_config()):
                environment = locate.child_environment(name, TOKEN)
                for variable in locate.ANTHROPIC_ENV_KEYS:
                    self.assertNotIn(variable, environment, name)

    def test_a_contributor_who_pays_by_api_key_can_keep_it(self):
        ambient = {"ANTHROPIC_API_KEY": "sk-mine", "ANTHROPIC_BASE_URL": "https://mine.invalid"}
        with mock.patch.dict(os.environ, ambient):
            environment = locate.child_environment("the paid model run", None, keep_anthropic_env=True)
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "sk-mine")
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "https://mine.invalid")

    def test_the_opt_in_is_a_config_field_and_is_off_by_default(self):
        self.assertFalse(Config().locate_keep_anthropic_env)
        self.assertTrue(Config(locate_keep_anthropic_env=True).locate_keep_anthropic_env)


class ReadOnlyCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.site_server = fakes.FakeWikiLean()
        self.addCleanup(self.site_server.close)
        overrides = fakes.private_home(self.directory)
        overrides[model.CLAUDE_CMD_ENV] = fakes.write_fake_claude(self.directory)
        overrides["WIKILEAN_BASE_URL"] = self.site_server.base_url
        self.env = mock.patch.dict(os.environ, overrides, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(secrets.ENV_TOKEN, None)
        secrets.store_token(TOKEN, prefer="file")

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(secrets, "_security", return_value=None):
            code = main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_status_json_is_a_read_only_survey_without_the_token(self):
        code, out = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0, out)
        found = json.loads(out)
        self.assertTrue(found["reachable"])
        self.assertEqual(found["role"], "agent")
        self.assertEqual(found["work"]["not_formalized"], 2)
        self.assertEqual(found["writes_per_minute"], 30)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.site_server.writes, [])

    def test_the_dashboard_snapshot_writes_nothing_and_shows_no_token(self):
        code, out = self.run_cli([])
        self.assertEqual(code, 0, out)
        self.assertIn("WikiLean contributor", out)
        self.assertIn("Fixture (agent)", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.site_server.writes, [])

    def claude_is_logged_in(self) -> None:
        """Claude Code's credentials file, under the test's own HOME. doctor
        only looks for it; nothing reads or writes a credential."""
        credentials = Path(os.environ["HOME"]) / ".claude" / ".credentials.json"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text("{}", encoding="utf-8")

    def test_doctor_spends_nothing_and_checks_the_sandbox(self):
        self.claude_is_logged_in()
        code, out = self.run_cli(["doctor", "--json"])
        checks = {check["name"]: check for check in json.loads(out)}
        self.assertTrue(checks["site reachable"]["ok"], out)
        self.assertTrue(checks["token accepted"]["ok"], out)
        self.assertTrue(checks["agent token stored"]["ok"])
        self.assertTrue(checks["the model sandbox"]["ok"], out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.site_server.writes, [])
        # Everything that matters passed, so the exit code says so — and the
        # locate unit, which has NOT been set up, must not change that: it is
        # the one check marked non-fatal.
        self.assertFalse(checks["locate unit"]["ok"])
        self.assertFalse(checks["locate unit"]["fatal"])
        self.assertEqual(code, 0, out)

    def test_doctor_names_a_token_that_is_not_an_agent(self):
        self.claude_is_logged_in()
        self.site_server.role = "user"
        code, out = self.run_cli(["doctor", "--json"])
        checks = {check["name"]: check for check in json.loads(out)}
        self.assertFalse(checks["token accepted"]["ok"])
        self.assertIn("/agents", checks["token accepted"]["note"])
        # And a failed fatal check is what the exit code is for.
        self.assertEqual(code, 1, out)

    def test_the_doctor_report_agrees_with_the_json_exit_code(self):
        from wikilean.doctor import Check, report

        printed: list[str] = []
        self.assertEqual(report([Check("a", True), Check("b", False, fatal=False)], log=printed.append), 0)
        self.assertEqual(report([Check("a", True), Check("b", False)], log=printed.append), 1)
        self.assertIn("1 check(s) failed", "\n".join(printed))

    def test_work_warns_that_the_web_is_on_and_no_web_silences_it(self):
        from wikilean.cli import WEB_NOTICE

        code, out = self.run_cli(["work", "--dry-run"])
        self.assertIn(WEB_NOTICE, out)
        self.assertIn("--no-web", WEB_NOTICE)
        code, out = self.run_cli(["work", "--dry-run", "--no-web"])
        self.assertNotIn(WEB_NOTICE, out)

    def test_a_config_this_cannot_read_turns_the_web_off_and_says_so(self):
        from wikilean.config import config_path, config_problem

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for written in ('{"web": false,}', '[]', '{"web": "false"}'):
            with self.subTest(written=written):
                path.write_text(written, encoding="utf-8")
                self.assertFalse(Config.load().web)
                self.assertIn("web is off", config_problem())
                code, out = self.run_cli(["doctor", "--json"])
                checks = {check["name"]: check for check in json.loads(out)}
                self.assertFalse(checks["config.json"]["ok"])
                self.assertFalse(checks["config.json"]["fatal"])
                self.assertTrue(checks["web tools"]["note"].startswith("off"))
        path.unlink()
        self.assertTrue(Config.load().web)
        self.assertIsNone(config_problem())

    def test_work_without_a_token_refuses_before_any_request(self):
        secrets.delete_token()
        code, out = self.run_cli(["work"])
        self.assertEqual(code, 2)
        self.assertEqual(self.site_server.calls, [])


class PrivateHomeTest(unittest.TestCase):
    """fakes.private_home must decide the whole environment doctor reads.

    `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN` are Claude Code's own
    overrides, so they are set on exactly the machines this CLI is for — and
    doctor.py consults both BEFORE it looks under HOME. A test that let either
    through would be answered by the host rather than by itself: green where
    nothing is set, red (or vacuously green) where something is.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        hostile = self.directory / "hostile"      # a config dir with no credentials in it
        hostile.mkdir()
        # The host, as unhelpful as it is allowed to be.
        self.host = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(hostile),
                                                 "CLAUDE_CODE_OAUTH_TOKEN": "ambient"}, clear=False)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.env = mock.patch.dict(os.environ, fakes.private_home(self.directory), clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_host_s_claude_code_environment_does_not_reach_doctor(self):
        from wikilean import doctor

        config = doctor.claude_config_dir()
        self.assertEqual(config, Path(os.environ["HOME"]) / ".claude")
        credentials = config / ".credentials.json"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text("{}", encoding="utf-8")
        ok, note = doctor.claude_logged_in()
        self.assertTrue(ok)
        # The test's own file is the reason, not the ambient OAuth token: that
        # one would make the answer True without the file existing at all.
        self.assertEqual(note, str(credentials))


if __name__ == "__main__":
    unittest.main()
