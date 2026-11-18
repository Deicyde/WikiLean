#!/usr/bin/env python3
"""The agent token: where it is kept, and the places it must never appear."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes
from wikilean import journal, secrets
from wikilean.cli import main

TOKEN = "wla_" + "A" * 43


class TokenShapeTest(unittest.TestCase):
    def test_only_a_wikilean_agent_token_is_accepted(self):
        self.assertTrue(secrets.valid_token(TOKEN))
        self.assertTrue(secrets.valid_token("wla_" + "-_aZ09" * 7 + "x"))
        for bad in ("", "wla_", "wla_short", "x" * 47, "wla_" + "A" * 44,
                    "wla_" + "A" * 42 + "!", "Bearer " + TOKEN):
            with self.subTest(bad=bad[:12]):
                self.assertFalse(secrets.valid_token(bad))

    def test_the_fingerprint_does_not_contain_the_token(self):
        printed = secrets.fingerprint(TOKEN)
        self.assertEqual(len(printed), 12)
        self.assertNotIn(printed, TOKEN)
        self.assertNotIn(TOKEN[4:], printed)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, fakes.private_home(Path(self.tmp.name)), clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(secrets.ENV_TOKEN, None)

    def test_the_file_store_is_private_and_round_trips(self):
        self.assertEqual(secrets.store_token(TOKEN, prefer="file"), "file")
        path = secrets.token_file()
        self.assertEqual(fakes.mode_of(path), 0o600)
        self.assertEqual(fakes.mode_of(path.parent), 0o700)
        found = secrets.load_token()
        self.assertIsNotNone(found)
        self.assertEqual(found.token, TOKEN)
        self.assertEqual(found.source, "file")

    def test_logout_removes_it(self):
        secrets.store_token(TOKEN, prefer="file")
        self.assertIn("file", secrets.delete_token())
        self.assertIsNone(secrets.load_token())

    def test_a_malformed_token_is_refused_before_it_is_written(self):
        with self.assertRaises(secrets.TokenError):
            secrets.store_token("not-a-token", prefer="file")
        self.assertFalse(secrets.token_file().exists())

    def test_the_environment_wins_and_is_labelled(self):
        secrets.store_token(TOKEN, prefer="file")
        other = "wla_" + "B" * 43
        with mock.patch.dict(os.environ, {secrets.ENV_TOKEN: other}):
            found = secrets.load_token()
        self.assertEqual((found.token, found.source), (other, "environment"))

    def test_the_keychain_is_asked_for_the_item_not_told_the_token(self):
        """On a Mac the token travels on the child's stdin, never in argv."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(secrets, "_security", return_value="/usr/bin/security"), \
                mock.patch.object(secrets.subprocess, "run", side_effect=fake_run):
            self.assertEqual(secrets.store_token(TOKEN, prefer="keychain"), "keychain")
        argv, kwargs = calls[-1]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(kwargs["input"], TOKEN + "\n")
        self.assertIn("add-generic-password", argv)

    def test_a_keychain_refusal_under_auto_falls_back_to_the_file(self):
        with mock.patch.object(secrets, "_security", return_value="/usr/bin/security"), \
                mock.patch.object(secrets, "_keychain_write", side_effect=secrets.TokenError("locked")):
            self.assertEqual(secrets.store_token(TOKEN, prefer="auto"), "file")
        self.assertEqual(secrets.load_token().token, TOKEN)


class LoginCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        overrides = fakes.private_home(Path(self.tmp.name))
        self.env = mock.patch.dict(os.environ, overrides, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(secrets.ENV_TOKEN, None)

    def _login(self, token: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(token + "\n")), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(secrets, "_security", return_value=None):
            code = main(["login", "--stdin", "--store", "file"])
        return code, out.getvalue(), err.getvalue()

    def test_the_token_is_never_printed_and_never_journalled(self):
        code, out, err = self._login(TOKEN)
        self.assertEqual(code, 0, err)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, err)
        text = journal.journal_path().read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        entry = json.loads(text.strip().splitlines()[-1])
        self.assertEqual(entry["event"], "login")
        self.assertEqual(entry["fingerprint"], secrets.fingerprint(TOKEN))

    def test_a_bad_token_is_refused_without_being_echoed(self):
        code, out, err = self._login("wla_nope")
        self.assertEqual(code, 2)
        self.assertNotIn("wla_nope", out + err)
        self.assertIn("/agents", err)

    def test_the_token_is_not_an_argument_of_the_login_command(self):
        """There is no flag that takes a token: argv cannot carry one."""
        from wikilean.cli import build_parser

        login = build_parser()._subparsers._group_actions[0].choices["login"]
        taking_values = [action.dest for action in login._actions
                         if action.nargs != 0 and action.dest not in ("help",)]
        self.assertEqual(taking_values, ["store"])

    def test_the_journal_redacts_a_token_that_reaches_it_anyway(self):
        journal.write("test", note=f"Authorization: Bearer {TOKEN}", nested={"token": TOKEN},
                      listed=[f"see {TOKEN}"])
        text = journal.journal_path().read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertIn("[redacted]", text)


if __name__ == "__main__":
    unittest.main()
