#!/usr/bin/env python3
"""Hermetic tests for wiki/scripts/backup-d1.sh, with stub wrangler (via npx) and age.

The script must refuse to export when age or the backup recipients are missing or
invalid, and no run, failed or successful, may leave a plaintext dump or a
.partial file behind. The stubs make no network calls; PATH holds only them and
the coreutils the script needs, so a real age or wrangler is never reached.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "wiki" / "scripts" / "backup-d1.sh"
BASH = shutil.which("bash") or "/bin/bash"
TOOLS = ("cat", "cut", "date", "du", "mkdir", "mktemp", "mv", "rm")
PLAINTEXT = "-- wikilean plaintext marker\nCREATE TABLE articles (slug TEXT);\n"
RECIPIENT = "age1" + "q" * 58

# Each stub appends its argv (and what it read) to $STUB_LOG as one JSON line.
NPX_STUB = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"tool": "npx", "argv": args}) + "\n")
outputs = [arg for arg in args if arg.startswith("--output=")]
if args[:5] != ["wrangler", "d1", "export", "wikilean", "--remote"] or len(args) != 6 or len(outputs) != 1:
    sys.exit("npx stub: unexpected arguments %r" % (args,))
mode = os.environ.get("STUB_EXPORT", "ok")
if mode == "fail":
    sys.exit("stub wrangler: authentication error")
with open(outputs[0].split("=", 1)[1], "w") as dump:
    if mode != "empty":
        dump.write(os.environ["STUB_PLAINTEXT"])
sys.exit(1 if mode == "fail-after-write" else 0)
'''

AGE_STUB = r'''
import hashlib, json, os, sys
args = sys.argv[1:]
if len(args) < 4 or args[0] != "-R" or args[2] != "-o":
    sys.exit("age stub: unexpected arguments %r" % (args,))
recipients = [line for line in open(args[1]).read().splitlines() if line.strip()]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"tool": "age", "argv": args, "recipients": recipients}) + "\n")
for line in recipients:
    if line.startswith("ecdsa-sha2-"):
        # Real age warns about SSH key types it cannot use and skips them.
        print("age: warning: ignoring unsupported SSH key of type ecdsa-sha2-nistp256", file=sys.stderr)
    elif not line.startswith("age1"):
        sys.exit("age: error: unknown recipient type: %r" % line)
if args[3] == "/dev/null" and len(args) == 4:
    # Real age reads the plaintext from stdin here, so the check must redirect it.
    if sys.stdin.read():
        sys.exit("age stub: the recipient check read the caller's stdin")
    sys.exit(0)
if len(args) != 5:
    sys.exit("age stub: unexpected arguments %r" % (args,))
with open(args[4], "rb") as source:
    digest = hashlib.sha256(source.read()).hexdigest()
with open(args[3], "w") as encrypted:
    encrypted.write("age-encryption.org/v1\n")
    if os.environ.get("STUB_ENCRYPT") == "fail-after-write":
        sys.exit("age: error: write failed")
    encrypted.write("stub ciphertext " + digest + "\n")
'''


class BackupScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Path(tempfile.mkdtemp(prefix="backup-d1-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.sandbox)
        self.tools = self.sandbox / "tools"
        self.stubs = self.sandbox / "stubs"
        self.stubs_without_age = self.sandbox / "stubs-without-age"
        for directory in (self.tools, self.stubs, self.stubs_without_age):
            directory.mkdir()
        for name in TOOLS:
            found = shutil.which(name)
            self.assertIsNotNone(found, f"{name} is required to run backup-d1.sh")
            (self.tools / name).symlink_to(found)
        for directory, name, body in (
            (self.stubs, "npx", NPX_STUB),
            (self.stubs, "age", AGE_STUB),
            (self.stubs_without_age, "npx", NPX_STUB),
        ):
            stub = directory / name
            stub.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
            stub.chmod(0o755)
        self.tmp = self.sandbox / "tmp"
        self.cwd = self.sandbox / "cwd"
        self.tmp.mkdir()
        self.cwd.mkdir()
        self.out = self.sandbox / "out"
        self.log = self.sandbox / "stub-log.jsonl"
        self.log.touch()

    def run_script(
        self,
        *,
        recipients: str | None = RECIPIENT,
        export: str = "ok",
        encrypt: str = "ok",
        with_age: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        stubs = self.stubs if with_age else self.stubs_without_age
        env = {
            "PATH": f"{stubs}{os.pathsep}{self.tools}",
            "HOME": str(self.sandbox),
            "TMPDIR": str(self.tmp),
            "STUB_LOG": str(self.log),
            "STUB_EXPORT": export,
            "STUB_ENCRYPT": encrypt,
            "STUB_PLAINTEXT": PLAINTEXT,
        }
        if recipients is not None:
            env["BACKUP_AGE_RECIPIENTS"] = recipients
        result = subprocess.run(
            [BASH, str(SCRIPT), str(self.out)],
            cwd=self.cwd,
            env=env,
            input="caller stdin that age must not read\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotIn(PLAINTEXT, result.stdout + result.stderr, "the dump must never be printed")
        return result

    def calls(self, tool: str) -> list[dict]:
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["tool"] == tool]

    def assert_no_plaintext_or_partial(self) -> None:
        marker = PLAINTEXT.encode("utf-8")
        for path in self.sandbox.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.parent in (self.stubs, self.stubs_without_age):
                continue
            with self.subTest(path=str(path.relative_to(self.sandbox))):
                self.assertFalse(path.name.endswith(".partial"))
                self.assertFalse(path.name.endswith(".sql"))
                self.assertNotIn(marker, path.read_bytes())
        self.assertEqual(list(self.tmp.iterdir()), [], "the private work directory must be removed")

    def assert_refused_before_export(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.calls("npx"), [], "nothing may be exported")
        self.assertFalse(self.out.exists(), "the destination must not be created")
        self.assert_no_plaintext_or_partial()

    def assert_failed_without_backup(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls("npx")), 1)
        self.assertEqual(list(self.out.iterdir()), [], "a failed run must leave no backup file")
        self.assert_no_plaintext_or_partial()

    def test_missing_age_refuses_before_export(self):
        result = self.run_script(with_age=False)
        self.assert_refused_before_export(result, "age not found")

    def test_unset_recipients_refuse_before_export(self):
        result = self.run_script(recipients=None)
        self.assert_refused_before_export(result, "BACKUP_AGE_RECIPIENTS is empty")
        self.assertEqual(self.calls("age"), [])

    def test_blank_recipients_refuse_before_export(self):
        for recipients in ("", " \n\t\n "):
            with self.subTest(recipients=recipients):
                result = self.run_script(recipients=recipients)
                self.assert_refused_before_export(result, "BACKUP_AGE_RECIPIENTS is empty")
                self.assertEqual(self.calls("age"), [])

    def test_invalid_recipients_refuse_before_export(self):
        for recipients in ("not-a-key", f"{RECIPIENT}\nAGE-SECRET-KEY-1QQQQ"):
            with self.subTest(recipients=recipients):
                result = self.run_script(recipients=recipients)
                self.assert_refused_before_export(result, "not a valid age recipient list")

    def test_recipient_age_would_skip_refuses_before_export(self):
        result = self.run_script(recipients=f"{RECIPIENT}\necdsa-sha2-nistp256 AAAAE2VjZHNh spare")
        self.assert_refused_before_export(result, "refusing to export")
        self.assertIn("unsupported SSH key", result.stderr)

    def test_export_failure_leaves_nothing(self):
        for export in ("fail", "fail-after-write"):
            with self.subTest(export=export):
                self.log.write_text("", encoding="utf-8")
                result = self.run_script(export=export)
                self.assert_failed_without_backup(result)
                self.assertEqual(len(self.calls("age")), 1, "only the recipient check may run")

    def test_empty_export_is_not_encrypted(self):
        result = self.run_script(export="empty")
        self.assert_failed_without_backup(result)
        self.assertIn("empty export", result.stderr)
        self.assertEqual(len(self.calls("age")), 1, "only the recipient check may run")

    def test_encryption_failure_leaves_no_partial(self):
        result = self.run_script(encrypt="fail-after-write")
        self.assert_failed_without_backup(result)
        self.assertEqual(len(self.calls("age")), 2)

    def test_success_keeps_only_the_encrypted_backup(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (export,) = self.calls("npx")
        dump = Path(export["argv"][5].split("=", 1)[1])
        self.assertTrue(dump.is_relative_to(self.tmp), "the plaintext must stay in the private temp dir")
        check, encrypt = self.calls("age")
        self.assertEqual(check["argv"][2:], ["-o", "/dev/null"])
        self.assertEqual(encrypt["recipients"], [RECIPIENT])
        self.assertEqual(encrypt["argv"][4], str(dump))
        (backup,) = self.out.iterdir()
        self.assertRegex(backup.name, r"^wikilean-\d{8}T\d{6}Z\.sql\.age$")
        self.assertEqual(encrypt["argv"][3], f"{backup}.partial")
        self.assertTrue(backup.read_text(encoding="utf-8").startswith("age-encryption.org/v1\n"))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode) & 0o077, 0, "no group or other access")
        self.assertIn(f"wrote {backup}", result.stdout)
        self.assert_no_plaintext_or_partial()


if __name__ == "__main__":
    unittest.main()
