"""Safety checks for the manual native-client fixture; no SDK or model runs."""
import contextlib
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "mac_client_fixture.py"


class HarnessSafety(unittest.TestCase):
    def load(self):
        return runpy.run_path(str(PROGRAM), run_name="fixture_import")

    def test_import_starts_no_process_or_server(self):
        with patch("subprocess.run", side_effect=AssertionError("process started")), \
                patch("http.server.ThreadingHTTPServer", side_effect=AssertionError("server started")):
            namespace = self.load()
        self.assertTrue(callable(namespace["main"]))

    def test_an_api_key_refuses_before_any_work(self):
        main = self.load()["main"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new-case"
            with patch.object(sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-credential"}, clear=True), \
                    patch("subprocess.run", side_effect=AssertionError("process started")), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(["--root", str(root)])
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(root.exists())

    def test_normal_entry_uses_the_network_wrapper_and_drops_token_environment(self):
        main = self.load()["main"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new-case"
            with patch.object(sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "WIKILEAN_TEST_TOKEN": "not-a-credential"}, clear=True), \
                    patch.object(Path, "is_file", return_value=True), \
                    patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as launch:
                self.assertEqual(main(["--root", str(root)]), 0)
            launch.assert_called_once()
            command = launch.call_args.args[0]
            self.assertEqual(command[0], "/usr/bin/sandbox-exec")
            self.assertIn("--_confined", command)
            self.assertNotIn("WIKILEAN_TEST_TOKEN", launch.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", launch.call_args.kwargs["env"])
            self.assertFalse(root.exists())

    def test_non_mac_refuses_before_a_client_starts(self):
        main = self.load()["main"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new-case"
            with patch.object(sys, "platform", "linux"), \
                    patch("subprocess.run", side_effect=AssertionError("process started")), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(["--root", str(root)])
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
