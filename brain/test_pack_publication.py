#!/usr/bin/env python3
"""Real-kernel tests for exclusive publication of sealed pack directories."""
from __future__ import annotations

import errno
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent / "tools"
sys.path.insert(0, str(TOOLS))
import compile_offline_pack_v2 as compiler  # noqa: E402

SUPPORTED = sys.platform == "darwin" or sys.platform.startswith("linux")
DARWIN_UNPRIVILEGED = sys.platform == "darwin" and os.geteuid() != 0


@unittest.skipUnless(SUPPORTED, "exclusive directory publication supports Darwin and Linux")
class PackPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name)
        self.store.chmod(0o700)
        self.store_descriptor = os.open(self.store, os.O_RDONLY | os.O_DIRECTORY)
        self.source = self.store / ".candidate"
        self.target = self.store / "destination"
        self.make_sealed(self.source, b'{"fixture":"candidate"}\n')
        self.source_inode = self.source.stat().st_ino

    def tearDown(self) -> None:
        os.close(self.store_descriptor)
        # A killed publisher intentionally leaves its root non-searchable.
        # Only test-owned paths are restored for TemporaryDirectory cleanup.
        self.store.chmod(0o700)
        for directory, names, _files in os.walk(self.store):
            Path(directory).chmod(0o700)
            for name in names:
                (Path(directory) / name).chmod(0o700)
        self.temporary.cleanup()

    def make_sealed(self, root: Path, payload: bytes) -> None:
        root.mkdir(mode=0o700)
        manifest = root / "offline-pack.json"
        manifest.write_bytes(payload)
        manifest.chmod(0o444)
        root.chmod(0o555)

    def publish(self) -> None:
        compiler._publish_no_replace(
            self.store_descriptor, self.source.name, self.target.name,
        )

    def assert_sealed(self, root: Path, expected: bytes = b'{"fixture":"candidate"}\n') -> None:
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((root / "offline-pack.json").stat().st_mode), 0o444)
        self.assertEqual((root / "offline-pack.json").read_bytes(), expected)
        compiler._verify_read_only_tree(root)

    def test_readonly_directory_publishes_exclusively_with_same_inode_and_contents(self) -> None:
        self.publish()
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.stat().st_ino, self.source_inode)
        self.assert_sealed(self.target)

    def test_existing_destination_is_unchanged_and_source_mode_is_restored(self) -> None:
        existing = b'{"fixture":"existing"}\n'
        self.make_sealed(self.target, existing)
        target_inode = self.target.stat().st_ino
        with self.assertRaises(compiler._DestinationExists):
            self.publish()
        self.assertEqual(self.target.stat().st_ino, target_inode)
        self.assertEqual(self.source.stat().st_ino, self.source_inode)
        self.assert_sealed(self.target, existing)
        self.assert_sealed(self.source)

    def test_primitive_failure_restores_source_mode(self) -> None:
        with mock.patch.object(
            compiler, "_rename_no_replace", side_effect=OSError(errno.EIO, "injected"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                self.publish()
        self.assertFalse(self.target.exists())
        self.assert_sealed(self.source)

    def test_failure_after_successful_rename_reseals_the_retained_inode(self) -> None:
        real_rename = compiler._rename_no_replace

        def renamed_then_failed(store_descriptor: int, source_name: str, target_name: str) -> None:
            real_rename(store_descriptor, source_name, target_name)
            raise OSError(errno.EIO, "injected after rename")

        with mock.patch.object(compiler, "_rename_no_replace", side_effect=renamed_then_failed):
            with self.assertRaisesRegex(OSError, "injected after rename"):
                self.publish()
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.stat().st_ino, self.source_inode)
        self.assert_sealed(self.target)

    @unittest.skipUnless(DARWIN_UNPRIVILEGED, "Darwin permission boundary requires a non-root user")
    def test_final_path_cannot_be_read_during_darwin_rename_seam(self) -> None:
        real_rename = compiler._rename_no_replace
        checked = False

        def inspect_while_unsealed(store_descriptor: int, source_name: str, target_name: str) -> None:
            nonlocal checked
            self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o600)
            real_rename(store_descriptor, source_name, target_name)
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
            with self.assertRaises(PermissionError):
                compiler.contracts.load_canonical_json(self.target / "offline-pack.json")
            with self.assertRaisesRegex(compiler.PackCompilationError, "read-only directory"):
                compiler._verify_read_only_tree(self.target)
            checked = True

        with mock.patch.object(compiler, "_rename_no_replace", side_effect=inspect_while_unsealed):
            self.publish()
        self.assertTrue(checked)
        self.assert_sealed(self.target)

    @unittest.skipUnless(DARWIN_UNPRIVILEGED, "Darwin SIGKILL boundary requires a non-root user")
    def test_sigkill_after_rename_leaves_inaccessible_rejected_target(self) -> None:
        child_code = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import compile_offline_pack_v2 as compiler
store = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY)
original = compiler._rename_no_replace
def stop_after_rename(parent, source, target):
    original(parent, source, target)
    print("RENAMED", flush=True)
    signal.pause()
    raise RuntimeError("publisher unexpectedly resumed")
compiler._rename_no_replace = stop_after_rename
compiler._publish_no_replace(store, sys.argv[3], sys.argv[4])
'''
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", child_code, str(TOOLS), str(self.store),
             self.source.name, self.target.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            ready, _write, _error = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "child did not reach the post-rename crash seam")
            line = process.stdout.readline()
            self.assertEqual(line, b"RENAMED\n")
            process.send_signal(signal.SIGKILL)
            _stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL, stderr.decode("utf-8", "replace"))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.stat().st_ino, self.source_inode)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        with self.assertRaises(PermissionError):
            compiler.contracts.load_canonical_json(self.target / "offline-pack.json")
        with self.assertRaisesRegex(compiler.PackCompilationError, "read-only directory"):
            compiler._verify_read_only_tree(self.target)


if __name__ == "__main__":
    unittest.main()
