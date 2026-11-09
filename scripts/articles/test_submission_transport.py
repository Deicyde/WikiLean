#!/usr/bin/env python3
"""Offline transport checks plus a loopback integration using the real Worker and validator."""
from __future__ import annotations

import io
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import submission_transport as transport
import submit
import intake_worker

FIXTURE = ROOT / "scripts/pipeline/fixtures/recordset"
NOW = "2026-11-09T00:00:00Z"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.body, self.seal = transport.prepare(FIXTURE, received_at=NOW)

    def verify(self, body=None, seal=None):
        body = self.body if body is None else body
        return transport.verify_upload(body, machine_id="fixture-agent", received_at=NOW,
                                       body_sha256=transport.sha(body), seal_sha256=seal or self.seal)

    def test_exact_sealed_bytes_and_normative_rows_round_trip(self):
        accepted = self.verify()
        self.assertIsInstance(accepted, transport.intake.Accepted)
        self.assertGreater(len(accepted.records), 0)
        for path, data in transport.unpack(self.body).items():
            self.assertEqual(data, (FIXTURE / path).read_bytes())
        self.assertEqual(accepted.submission["state"], "accepted")

    def test_bad_record_is_rejected_by_the_original_checker_not_client_claims(self):
        data = json.loads(self.body)
        path = next(p for p in data["files"] if p.startswith("records/"))
        data["files"][path] = '{"status":"invented"}\n'
        seal = json.loads(data["files"]["SEAL.json"])
        seal["files"][path] = transport.sha(data["files"][path].encode())
        data["files"]["SEAL.json"] = json.dumps(seal)
        result = self.verify(json.dumps(data).encode(), transport.sha(data["files"]["SEAL.json"].encode()))
        self.assertIsInstance(result, transport.intake.Refusal)
        self.assertEqual(result.code, "record_set")

    def test_path_traversal_and_extra_files_never_reach_disk(self):
        for path in ("../escape", "/tmp/escape", "records/../escape", "records\\escape", "article.html"):
            with self.subTest(path=path):
                data = json.loads(self.body)
                data["files"][path] = "bad"
                with patch.object(transport.tempfile, "TemporaryDirectory", side_effect=AssertionError("unpacking unsafe paths")):
                    self.assertEqual(self.verify(json.dumps(data).encode()).code, "envelope")

    def test_duplicate_keys_invalid_utf8_and_unpaired_surrogates_are_refused(self):
        for body in (b'{"schema":"x","schema":"x","files":{}}', b'\xff',
                     b'{"schema":"wikilean.submission-upload/1","files":{"SEAL.json":"\\ud800"}}'):
            self.assertEqual(self.verify(body).code, "envelope")

    def test_digest_and_size_limits(self):
        result = transport.verify_upload(self.body, machine_id="test", received_at=NOW,
                                         body_sha256="0" * 64, seal_sha256=self.seal)
        self.assertEqual(result.code, "body_digest")
        self.assertEqual(self.verify(seal="0" * 64).code, "idempotency_key")
        self.assertEqual(self.verify(b"x" * (transport.MAX_BYTES + 1)).code, "envelope")

    def test_preview_never_reads_a_token_or_calls_the_network(self):
        with patch.object(transport, "request", side_effect=AssertionError("network")), patch.dict(os.environ, {}, clear=True):
            result = submit.submit(FIXTURE)
        self.assertTrue(result["dry_run"])

    def test_symlinks_are_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "set"
            shutil.copytree(FIXTURE, root)
            (root / "outside").symlink_to(FIXTURE / "SEAL.json")
            with self.assertRaises(transport.intake.build_store.ArticlesError):
                transport.prepare(root, received_at=NOW)

    def test_no_automatic_retry_of_http_refusals(self):
        for code in (403, 413, 422, 429, 503):
            with patch.object(transport, "request", return_value=(code, b'{"ok":false,"error":"refused"}')) as request:
                with self.assertRaises(transport.TransportError):
                    submit.submit(FIXTURE, execute=True, token="fixture-token")
                self.assertEqual(request.call_count, 1)

    def test_malformed_successes_are_refused_as_receipts_or_claims(self):
        for result in ({"ok": True}, {"ok": True, "submission": None},
                       {"ok": True, "submission": {"id": "wrong", "state": "accepted"}}):
            with patch.object(transport, "request", return_value=(200, json.dumps(result).encode())):
                with self.assertRaises(transport.TransportError):
                    submit.submit(FIXTURE, execute=True, token="fixture-token")
        for result in ({"ok": True}, {"ok": True, "job": []}, {"ok": True, "job": {"id": "../escape"}}):
            with patch.object(transport, "request", return_value=(200, json.dumps(result).encode())) as request:
                with self.assertRaises(transport.TransportError):
                    intake_worker.process_one(base_url=transport.BASE_URL, token="fixture-token")
                self.assertEqual(request.call_count, 1)

    def test_credentials_never_follow_redirects_or_insecure_origins(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid"))
        for value in ("http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?token=x"):
            with self.assertRaises(transport.TransportError):
                transport.origin(value)


class RealHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryFile()
        cls.server = subprocess.Popen([str(ROOT / "wiki/node_modules/.bin/tsx"),
                                       str(ROOT / "wiki/test/helpers/submission-server.ts")],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=cls.logs)
        ready, _, _ = select.select([cls.server.stdout], [], [], 20)
        if not ready:
            cls.server.terminate()
            raise RuntimeError("loopback Worker fixture did not start")
        cls.base = json.loads(cls.server.stdout.readline())["origin"]
        req = urllib.request.Request(cls.base + "/api/agents", data=b'{"name":"Fixture laptop"}',
                                     headers={"Content-Type": "application/json", "Cookie": "wl_dev_user=u-human"})
        with urllib.request.urlopen(req, timeout=10) as response:
            cls.token = json.load(response)["token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()
        cls.server.stdout.close()
        cls.logs.close()

    def test_real_upload_leased_validation_and_retry_receipt(self):
        queued = submit.submit(FIXTURE, base_url=self.base, execute=True, token=self.token)
        self.assertEqual(queued["submission"]["state"], "queued")
        validated = intake_worker.process_one(base_url=self.base, token="test-pipeline-token")
        self.assertEqual(validated["state"], "accepted")
        accepted = submit.status(queued["submission"]["id"], base_url=self.base, token=self.token)
        self.assertEqual(accepted["submission"]["state"], "accepted")
        self.assertGreater(accepted["submission"]["verdict"]["records"], 0)
        self.assertFalse(accepted["submission"]["published"])
        again = submit.submit(FIXTURE, base_url=self.base, execute=True, token=self.token)
        self.assertEqual(again, accepted)
        self.assertTrue(intake_worker.process_one(base_url=self.base, token="test-pipeline-token")["idle"])
        self.assertNotIn(self.token, json.dumps(accepted))

    def test_malicious_but_digest_correct_upload_is_rejected_without_operator(self):
        body, _ = transport.prepare(FIXTURE, received_at=NOW)
        data = json.loads(body)
        seal = json.loads(data["files"]["SEAL.json"])
        seal["skill_commit"] = "not-a-commit"
        data["files"]["SEAL.json"] = json.dumps(seal)
        body = json.dumps(data).encode()
        code, response = transport.request(self.base, "/api/submissions", self.token, body=body,
            headers={"Idempotency-Key": transport.sha(data["files"]["SEAL.json"].encode()), "Content-Digest": transport.sha(body)})
        self.assertEqual(code, 202)
        receipt = json.loads(response)
        outcome = intake_worker.process_one(base_url=self.base, token="test-pipeline-token")
        self.assertEqual(outcome["state"], "rejected")
        result = submit.status(receipt["submission"]["id"], base_url=self.base, token=self.token)
        self.assertEqual(result["submission"]["verdict"]["code"], "seal")


if __name__ == "__main__":
    unittest.main()
