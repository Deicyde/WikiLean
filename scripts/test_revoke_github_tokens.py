#!/usr/bin/env python3
"""revoke_github_tokens finds tokens in every source, never shows one, and posts only on --submit.

The fake tokens are assembled at run time so no token-shaped literal sits in the repo.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import revoke_github_tokens as rgt  # noqa: E402


def fake(prefix: str, seed: int, length: int = 36) -> str:
    body = (f"{seed:04d}" + "Ab9" * 100)[:length]
    return f"{prefix}_{body}"


GHO = fake("gho", 1)
GHU = fake("ghu", 2)
GHR = fake("ghr", 3, 76)
PAT = "github_pat_" + ("11AbCdEfGh" * 3)[:22] + "_" + ("Zz9" * 30)[:59]
OWN = fake("gho", 4)


def d1_json(rows: list) -> str:
    """What `wrangler d1 execute --json` prints for one successful statement."""
    return json.dumps([{"results": rows, "success": True, "meta": {"duration": 0.1}}])


class Response:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RevokeTests(unittest.TestCase):
    def setUp(self):
        # A test that reaches the network fails instead of posting to GitHub.
        guard = mock.patch.object(socket, "create_connection", side_effect=AssertionError("network call in a test"))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.posts = []
        self.urlopen = self.fake_urlopen
        self.d1_argv: list[list[str]] = []
        self.d1_env: dict[str, str] = {}
        self.d1_stdout = ""

    def write(self, name: str, text: str, gz: bool = False) -> Path:
        path = self.tmp / name
        path.write_bytes(gzip.compress(text.encode()) if gz else text.encode())
        return path

    def fake_run(self, argv, **kwargs):
        if argv[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(argv, 0, OWN + "\n", "")
        self.d1_argv.append(list(argv))
        self.d1_env = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, self.d1_stdout, "")

    def fake_urlopen(self, request, timeout=None):
        self.posts.append(request)
        return Response(202)

    def run_main(self, *argv: str, stdin: bytes = b"") -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(rgt.subprocess, "run", self.fake_run), \
                mock.patch.object(rgt.urllib.request, "urlopen", self.urlopen), \
                mock.patch.object(rgt.sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin))), \
                contextlib.redirect_stdout(out):
            code = rgt.main(list(argv))
        return code, out.getvalue()

    def assert_no_token(self, text: str):
        for token in (GHO, GHU, GHR, PAT, OWN):
            self.assertNotIn(token, text)
            self.assertNotIn(token[4:20], text)

    def test_dry_run_reports_digests_and_never_posts(self):
        dump = self.write("old.sql", f"INSERT INTO accounts VALUES('a','{GHO}','{GHR}');\n"
                                     f"INSERT INTO accounts VALUES('b','{GHU}',NULL);\n")
        code, out = self.run_main("--file", str(dump))
        self.assertEqual(code, 0)
        self.assertEqual(self.posts, [])
        self.assert_no_token(out)
        for token in (GHO, GHU, GHR):
            self.assertIn(rgt.digest(token), out)
        self.assertIn("3 distinct tokens (1 gho, 1 ghr, 1 ghu)", out)
        self.assertIn("dry run", out)

    def test_sources_merge_and_dedupe(self):
        sql = self.write("a.sql", f"'{GHO}' '{PAT}'")
        gz = self.write("b.sql.gz", f"'{GHO}' '{GHU}'", gz=True)
        self.d1_stdout = d1_json([{"access_token": GHU}, {"access_token": None}])
        code, out = self.run_main("--file", str(sql), "--file", str(gz), "--d1", "wikilean",
                                  "--d1-sql", "SELECT access_token FROM accounts")
        self.assertEqual(code, 0)
        self.assert_no_token(out)
        self.assertIn("3 distinct tokens (1 gho, 1 ghu, 1 github_pat)", out)
        line = next(l for l in out.splitlines() if rgt.digest(GHU) in l)
        self.assertIn(str(gz), line)
        self.assertIn("d1:wikilean", line)

    def test_stdin_source(self):
        code, out = self.run_main("--file", "-", stdin=f"x {GHO} y".encode())
        self.assertEqual(code, 0)
        self.assertIn(rgt.digest(GHO), out)

    def test_d1_argv_holds_only_sql_and_disables_wrangler_log_file(self):
        self.d1_stdout = d1_json([{"t": GHO}])
        # An inherited WRANGLER_LOG=warn empties wrangler 4.151.3's --json stdout, so it is dropped.
        with mock.patch.dict(os.environ, {"WRANGLER_LOG": "warn"}):
            self.run_main("--d1", "wikilean", "--d1-sql", "SELECT 1")
        (argv,) = self.d1_argv
        self.assertEqual(argv[-5:], ["wikilean", "--remote", "--json", "--command", "SELECT 1"])
        self.assertEqual(self.d1_env.get("WRANGLER_WRITE_LOGS"), "false")
        self.assertNotIn("WRANGLER_LOG", self.d1_env)

    def test_d1_empty_stdout_refuses_instead_of_reporting_no_tokens(self):
        # What wrangler 4.151.3 does under WRANGLER_LOG=warn: exit 0, nothing on stdout.
        self.d1_stdout = ""
        out = io.StringIO()
        with mock.patch.object(rgt.subprocess, "run", self.fake_run), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                rgt.main(["--d1", "wikilean", "--d1-sql", "SELECT 1"])
        self.assertIsInstance(raised.exception.code, str)  # a message, so the exit status is 1
        self.assertIn("refusing", raised.exception.code)
        self.assertNotIn("distinct tokens", out.getvalue())

    def test_d1_output_that_is_not_successful_results_refuses(self):
        for stdout in ("\n", "null", "{}", "[]", "[1]", f"{GHO}\n", json.dumps({"results": [{"t": GHO}]}),
                       json.dumps([{"results": [{"t": GHO}]}]),
                       json.dumps([{"results": [{"t": GHO}], "success": False}]),
                       json.dumps([{"success": True}]),
                       d1_json([{"t": GHO}]) + "\ntrailing"):
            with self.subTest(stdout=rgt.TOKEN.sub("[token]", stdout)[:60]):
                self.d1_stdout = stdout
                with self.assertRaises(SystemExit) as raised:
                    self.run_main("--d1", "wikilean", "--d1-sql", "SELECT 1")
                self.assertIn("refusing", raised.exception.code)
                self.assert_no_token(raised.exception.code)

    def test_d1_banner_before_the_json_is_read(self):
        self.d1_stdout = "wrangler 4.151.3\n" + d1_json([{"t": GHO}])
        code, out = self.run_main("--d1", "wikilean", "--d1-sql", "SELECT 1")
        self.assertEqual(code, 0)
        self.assertIn(rgt.digest(GHO), out)

    def test_d1_no_matching_rows_reports_none(self):
        self.d1_stdout = d1_json([])
        code, out = self.run_main("--d1", "wikilean", "--d1-sql", "SELECT 1")
        self.assertEqual(code, 0)
        self.assertIn("0 distinct tokens (none)", out)

    def test_d1_failure_does_not_echo_a_token(self):
        def failing(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", f"boom near {GHO}\n")
        with mock.patch.object(rgt.subprocess, "run", failing):
            with self.assertRaises(SystemExit) as raised:
                rgt.main(["--d1", "wikilean", "--d1-sql", "SELECT 1"])
        self.assertIn("[token]", str(raised.exception))
        self.assertNotIn(GHO, str(raised.exception))

    def test_own_gh_token_is_never_revoked(self):
        dump = self.write("old.sql", f"'{OWN}' '{GHO}'")
        code, out = self.run_main("--file", str(dump), "--submit")
        self.assertEqual(code, 0)
        self.assertIn("gh login token was found and is left alone", out)
        (request,) = self.posts
        self.assertEqual(json.loads(request.data)["credentials"], [GHO])

    def test_submit_posts_unauthenticated_batches(self):
        tokens = [fake("gho", i) for i in range(100, 100 + rgt.BATCH + 5)]  # none is OWN
        dump = self.write("many.sql", " ".join(tokens))
        code, out = self.run_main("--file", str(dump), "--submit")
        self.assertEqual(code, 0)
        self.assert_no_token(out)
        self.assertEqual(len(self.posts), 2)
        sent = []
        for request in self.posts:
            self.assertEqual(request.full_url, rgt.REVOKE_URL)
            self.assertEqual(request.get_method(), "POST")
            self.assertFalse(any(name.lower() == "authorization" for name, _ in request.header_items()))
            batch = json.loads(request.data)["credentials"]
            self.assertLessEqual(len(batch), rgt.BATCH)
            sent += batch
        self.assertEqual(sorted(sent), sorted(tokens))
        self.assertIn("GitHub answered 202, 202", out)

    def test_refuses_more_requests_than_the_hourly_limit(self):
        dump = self.write("x.sql", f"{GHO} {GHU}")
        with mock.patch.object(rgt, "BATCH", 1), mock.patch.object(rgt, "MAX_REQUESTS", 1):
            code, out = self.run_main("--file", str(dump), "--submit")
        self.assertEqual(code, 1)
        self.assertEqual(self.posts, [])
        self.assertIn("refusing", out)

    def test_http_error_fails_the_run(self):
        def reject(request, timeout=None):
            raise rgt.urllib.error.HTTPError(request.full_url, 422, "bad", {}, io.BytesIO(b""))
        self.urlopen = reject
        code, out = self.run_main("--file", str(self.write("x.sql", GHO)), "--submit")
        self.assertEqual(code, 1)
        self.assertIn("GitHub answered 422", out)

    def test_argument_errors(self):
        for argv in ([], ["--d1", "wikilean"], ["--file", str(self.tmp / "missing.sql")]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                rgt.main(argv)


if __name__ == "__main__":
    unittest.main()
