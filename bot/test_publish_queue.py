#!/usr/bin/env python3
"""publish_queue.py keeps the pipeline bearer off curl's argv.

A process's argv is visible to every user on the host (ps, /proc/<pid>/cmdline).
The bearer must reach curl on stdin and the body through a private 0600 file.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_queue  # noqa: E402

TOKEN = "pipeline-secret-value"


class PostQueueTest(unittest.TestCase):
    def run_post(self, token: str = TOKEN):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["input"] = kwargs.get("input")
            body = Path(argv[argv.index("--data-binary") + 1].removeprefix("@"))
            seen["body_path"] = body
            seen["body"] = body.read_text(encoding="utf-8")
            seen["mode"] = stat.S_IMODE(body.stat().st_mode)
            seen["dir_mode"] = stat.S_IMODE(body.parent.stat().st_mode)
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok":true}', stderr="")

        payload = {"items": [{"qid": "Q1", "decl": "Foo.bar", "status": "unreviewed"}]}
        with mock.patch.object(publish_queue.subprocess, "run", side_effect=fake_run):
            result = publish_queue.post_queue("https://wiki.example/api/queue", token, payload)
        return result, seen, payload

    def test_bearer_is_on_stdin_and_never_on_argv(self):
        result, seen, payload = self.run_post()
        self.assertEqual(result.stdout, '{"ok":true}')
        self.assertNotIn(TOKEN, " ".join(seen["argv"]))
        self.assertEqual(seen["input"], f"Authorization: Bearer {TOKEN}\n")
        argv = seen["argv"]
        self.assertEqual(argv[:5], ["curl", "-sS", "-X", "POST", "https://wiki.example/api/queue"])
        self.assertEqual(argv[argv.index("-H") + 1], "@-")
        self.assertIn("Content-Type: application/json", argv)
        self.assertEqual(json.loads(seen["body"]), payload)

    def test_body_file_is_private_and_removed(self):
        _, seen, _ = self.run_post()
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["dir_mode"] & 0o077, 0)
        self.assertFalse(seen["body_path"].exists())
        self.assertFalse(seen["body_path"].parent.exists())

    def test_refuses_a_token_that_would_inject_a_header(self):
        for bad in ("a\nX-Evil: 1", "a\rb"):
            with self.assertRaises(SystemExit), mock.patch.object(publish_queue.subprocess, "run") as run:
                publish_queue.post_queue("https://wiki.example/api/queue", bad, {"items": []})
            run.assert_not_called()

    def test_main_publishes_through_post_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            candidates = Path(raw) / "candidates.json"
            candidates.write_text(json.dumps([{"qid": "Q7", "label": "Seven", "decl": "Nat.seven"}]))
            argv = ["publish_queue.py", "--candidates", str(candidates), "--wiki", "https://wiki.example"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"WIKILEAN_API_TOKEN": TOKEN}), \
                    mock.patch.object(publish_queue, "wikidata_labels", return_value={}), \
                    mock.patch.object(publish_queue, "post_queue",
                                      return_value=subprocess.CompletedProcess([], 0, "ok", "")) as post, \
                    mock.patch("builtins.print"):
                publish_queue.main()
        url, token, payload = post.call_args.args
        self.assertEqual(url, "https://wiki.example/api/queue")
        self.assertEqual(token, TOKEN)
        self.assertEqual(payload["items"][0]["qid"], "Q7")


if __name__ == "__main__":
    unittest.main()
