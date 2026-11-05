#!/usr/bin/env python3
"""revoke_github_tokens finds tokens in every source, never shows one, and posts only on --submit.

The fake tokens are assembled at run time so no token-shaped literal sits in the repo.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
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


class Response:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RevokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.posts = []
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
                mock.patch.object(rgt.urllib.request, "urlopen", self.fake_urlopen), \
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
        self.d1_stdout = json.dumps([{"results": [{"access_token": GHU}, {"access_token": None}]}])
        code, out = self.run_main("--file", str(sql), "--file", str(gz), "--d1", "wikilean",
                                  "--d1-sql", "SELECT access_token FROM accounts")
        self.assertEqual(code, 0)
        self.assert_no_token(out)
        self.assertIn("3 distinct tokens (1 github_pat, 1 gho, 1 ghu)", out)
        line = next(l for l in out.splitlines() if rgt.digest(GHU) in l)
        self.assertIn(str(gz), line)
        self.assertIn("d1:wikilean", line)

    def test_stdin_source(self):
        code, out = self.run_main("--file", "-", stdin=f"x {GHO} y".encode())
        self.assertEqual(code, 0)
        self.assertIn(rgt.digest(GHO), out)

    def test_d1_argv_holds_only_sql_and_disables_wrangler_log_file(self):
        self.d1_stdout = json.dumps([{"results": [{"t": GHO}]}])
        self.run_main("--d1", "wikilean", "--d1-sql", "SELECT 1")
        (argv,) = self.d1_argv
        self.assertEqual(argv[-5:], ["wikilean", "--remote", "--json", "--command", "SELECT 1"])
        self.assertEqual(self.d1_env.get("WRANGLER_WRITE_LOGS"), "false")

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
        tokens = [fake("gho", i) for i in range(rgt.BATCH + 5)]
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
        dump = self.write("x.sql", GHO)
        with mock.patch.object(rgt.urllib.request, "urlopen", reject):
            code, out = self.run_main("--file", str(dump), "--submit")
        self.assertEqual(code, 1)
        self.assertIn("GitHub answered 422", out)

    def test_argument_errors(self):
        for argv in ([], ["--d1", "wikilean"], ["--file", str(self.tmp / "missing.sql")]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                rgt.main(argv)


if __name__ == "__main__":
    unittest.main()
