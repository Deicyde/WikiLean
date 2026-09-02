#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brain_deploy_journal as journal

HERE = Path(__file__).resolve().parent


class JournalFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir(mode=0o700)
        self.root = journal.validate_receipt_root(self.base / "receipts", self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class ReceiptRootTest(JournalFixture):
    def test_requires_absolute_external_private_owned_directory(self) -> None:
        with self.assertRaisesRegex(journal.ReceiptRootError, "absolute"):
            journal.validate_receipt_root("relative-receipts", self.repo)

        with self.assertRaisesRegex(journal.ReceiptRootError, "outside"):
            journal.validate_receipt_root(self.repo / "receipts", self.repo)
        private_ancestor = self.base / "private-ancestor"
        private_ancestor.mkdir(mode=0o700)
        nested_repo = private_ancestor / "checkout"
        nested_repo.mkdir(mode=0o700)
        with self.assertRaisesRegex(journal.ReceiptRootError, "outside"):
            journal.validate_receipt_root(private_ancestor, nested_repo)

        bad_permissions = self.base / "bad-permissions"
        bad_permissions.mkdir(mode=0o700)
        bad_permissions.chmod(0o750)
        with self.assertRaisesRegex(journal.ReceiptRootError, "0700"):
            journal.validate_receipt_root(bad_permissions, self.repo)

        linked_target = self.base / "linked-target"
        linked_target.mkdir(mode=0o700)
        linked_root = self.base / "linked-root"
        linked_root.symlink_to(linked_target, target_is_directory=True)
        with self.assertRaisesRegex(journal.ReceiptRootError, "symlink"):
            journal.validate_receipt_root(linked_root, self.repo)

        with mock.patch.object(journal.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(journal.ReceiptRootError, "owned"):
                journal.validate_receipt_root(self.root, self.repo)

    def test_creates_and_durably_returns_physical_root(self) -> None:
        self.assertTrue(self.root.is_absolute())
        self.assertEqual(self.root, self.root.resolve(strict=True))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertFalse(self.root.is_relative_to(self.repo))

    def test_target_marker_requires_explicit_initialization_and_pins_origin(self) -> None:
        uninitialized = journal.validate_receipt_root(
            self.base / "targeted-receipts", self.repo
        )
        with self.assertRaisesRegex(journal.ReceiptRootError, "initialized explicitly"):
            journal.validate_target_receipt_root(
                uninitialized, self.repo, "https://example.test"
            )
        initialized = journal.initialize_target_receipt_root(
            uninitialized, self.repo, "https://example.test/"
        )
        self.assertEqual(
            journal.validate_target_receipt_root(
                initialized, self.repo, "https://example.test"
            ),
            initialized,
        )
        marker = initialized / journal.RECEIPT_ROOT_MARKER
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o400)
        with self.assertRaisesRegex(journal.ReceiptRootError, "pinned to"):
            journal.validate_target_receipt_root(
                initialized, self.repo, "https://other.example"
            )

    def test_refuses_to_pin_a_root_with_existing_attempts(self) -> None:
        unmarked = journal.validate_receipt_root(self.base / "legacy-receipts", self.repo)
        with journal.PromotionLock(unmarked):
            journal.EventJournal.create_with_intent(
                unmarked, "legacy-attempt", {"candidate": "one"}
            )
        with self.assertRaisesRegex(journal.ReceiptRootError, "already contains"):
            journal.initialize_target_receipt_root(
                unmarked, self.repo, "https://example.test"
            )

    def test_cli_initializes_and_verifies_the_pinned_target(self) -> None:
        receipt = self.base / "cli-receipts"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = journal.main(
                [
                    "init",
                    "--receipt-dir",
                    str(receipt),
                    "--repo-root",
                    str(self.repo),
                    "--target-origin",
                    "https://example.test",
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["target_origin"], "https://example.test")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = journal.main(
                [
                    "verify",
                    "--receipt-dir",
                    str(receipt),
                    "--repo-root",
                    str(self.repo),
                    "--target-origin",
                    "https://example.test",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["incomplete_attempts"], [])


class PromotionLockTest(JournalFixture):
    def test_lock_is_non_reentrant_and_required_for_writes(self) -> None:
        with self.assertRaisesRegex(journal.JournalLockError, "required"):
            journal.EventJournal.create_with_intent(
                self.root, "attempt-one", {"candidate": "one"}
            )

        with journal.PromotionLock(self.root):
            created = journal.EventJournal.create_with_intent(
                self.root, "attempt-one", {"candidate": "one"}
            )
            self.assertTrue(created.incomplete)
            with self.assertRaisesRegex(journal.JournalLockError, "already held"):
                with journal.PromotionLock(self.root):
                    pass

    def test_lock_excludes_another_process_without_waiting(self) -> None:
        child = """
import sys
from brain_deploy_journal import JournalLockError, PromotionLock
try:
    with PromotionLock(sys.argv[1]):
        pass
except JournalLockError:
    raise SystemExit(23)
raise SystemExit(0)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(HERE)
        with journal.PromotionLock(self.root):
            result = subprocess.run(
                [sys.executable, "-c", child, str(self.root)],
                cwd=self.base,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 23, result.stderr)


class EventChainTest(JournalFixture):
    def test_create_with_intent_first_publishes_a_complete_chain(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root,
                "atomic-attempt",
                {"release_id": "sha256:" + "a" * 64},
                recorded_at="2026-09-01T12:00:00Z",
            )
        self.assertEqual([event["kind"] for event in deployment.events], ["intent"])
        self.assertTrue(deployment.incomplete)
        self.assertIsNotNone(deployment.chain_tip)
        self.assertEqual(
            [item.attempt_id for item in journal.list_incomplete_attempts(self.root)],
            ["atomic-attempt"],
        )

    def test_hash_chain_blobs_and_terminal_state_round_trip(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root,
                "attempt-20260901",
                {"release_id": "sha256:" + "a" * 64},
                recorded_at="2026-09-01T12:00:00Z",
            )
            intent = deployment.events[0]
            blob = deployment.append_blob(
                "wrangler-stdout",
                b"Current Version ID: 11111111-1111-1111-1111-111111111111\n",
                "text/plain; charset=utf-8",
            )
            result = deployment.append(
                "deploy_result",
                {"exit_code": 0, "stdout": blob},
                recorded_at="2026-09-01T12:00:01.250000Z",
            )
            final = deployment.append(
                "final_state",
                {"outcome": "deployed"},
                recorded_at="2026-09-01T12:00:02Z",
            )

            self.assertEqual(result["previous_event_id"], intent["event_id"])
            self.assertEqual(final["previous_event_id"], result["event_id"])
            self.assertEqual(deployment.chain_tip, final["event_id"])
            self.assertTrue(deployment.terminal)
            self.assertFalse(deployment.incomplete)
            with self.assertRaisesRegex(journal.JournalStateError, "already final"):
                deployment.append("observation", {})
            with self.assertRaisesRegex(journal.JournalStateError, "already final"):
                deployment.append_blob("late-output", b"late", "text/plain")

        loaded = journal.EventJournal.load(deployment.attempt_dir)
        self.assertTrue(loaded.terminal)
        self.assertEqual(loaded.chain_tip, final["event_id"])
        self.assertEqual(len(loaded.events), 3)
        event_files = sorted((loaded.attempt_dir / "events").iterdir())
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in event_files))
        self.assertTrue(all(not path.read_bytes().endswith(b"\n") for path in event_files))
        blob_path = loaded.attempt_dir / blob["path"]
        self.assertEqual(stat.S_IMODE(blob_path.stat().st_mode), 0o400)
        self.assertEqual(
            blob_path.read_bytes(),
            b"Current Version ID: 11111111-1111-1111-1111-111111111111\n",
        )

    def test_creation_requires_intent_and_attempt_id_is_safe(self) -> None:
        with journal.PromotionLock(self.root):
            with self.assertRaisesRegex(journal.JournalStateError, "attempt_id"):
                journal.EventJournal.create_with_intent(
                    self.root, "../escape", {"candidate": "one"}
                )
            deployment = journal.EventJournal.create_with_intent(
                self.root, "safe-attempt", {"candidate": "one"}
            )
            with self.assertRaisesRegex(journal.JournalStateError, "only as the first"):
                deployment.append("intent", {})

    def test_incomplete_scan_validates_all_attempts(self) -> None:
        with journal.PromotionLock(self.root):
            incomplete = journal.EventJournal.create_with_intent(
                self.root, "attempt-incomplete", {"candidate": "one"}
            )
            complete = journal.EventJournal.create_with_intent(
                self.root, "attempt-complete", {"candidate": "two"}
            )
            complete.append("final_state", {"outcome": "no_production_change"})

        found = journal.list_incomplete_attempts(self.root)
        self.assertEqual([item.attempt_id for item in found], ["attempt-incomplete"])
        self.assertTrue(found[0].incomplete)

    def test_event_tampering_and_noncanonical_bytes_are_rejected(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root, "attempt-tampered", {"candidate": "one"}
            )
        event_path = next((deployment.attempt_dir / "events").iterdir())
        original = json.loads(event_path.read_bytes())
        original["payload"]["candidate"] = "two"
        event_path.chmod(0o600)
        event_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
        event_path.chmod(0o400)
        with self.assertRaisesRegex(journal.JournalValidationError, "canonical JSON"):
            journal.EventJournal.load(deployment.attempt_dir)

    def test_blob_tampering_is_rejected_when_chain_is_loaded(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root, "attempt-blob-tampered", {"candidate": "one"}
            )
            blob = deployment.append_blob("status", b"original", "application/json")
            deployment.append("observation", {"status": blob})
        blob_path = deployment.attempt_dir / blob["path"]
        blob_path.chmod(0o600)
        blob_path.write_bytes(b"modified")
        blob_path.chmod(0o400)
        with self.assertRaisesRegex(journal.JournalValidationError, "filename/digest mismatch"):
            journal.EventJournal.load(deployment.attempt_dir)

    def test_unpublished_crash_temporary_is_not_part_of_the_chain(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root, "attempt-crash-temp", {"candidate": "one"}
            )
        pending = deployment.attempt_dir / "events" / ".pending-crash"
        pending.write_bytes(b'{"partial":')
        pending.chmod(0o600)
        loaded = journal.EventJournal.load(deployment.attempt_dir)
        self.assertEqual([event["kind"] for event in loaded.events], ["intent"])
        self.assertTrue(loaded.incomplete)

    def test_managed_symlinks_and_noncanonical_payloads_are_rejected(self) -> None:
        with journal.PromotionLock(self.root):
            with self.assertRaisesRegex(journal.JournalValidationError, "unsupported type"):
                journal.EventJournal.create_with_intent(
                    self.root, "attempt-invalid-float", {"fraction": 1.5}
                )
            cyclic: dict[str, object] = {}
            cyclic["self"] = cyclic
            with self.assertRaisesRegex(journal.JournalValidationError, "cyclic"):
                journal.EventJournal.create_with_intent(
                    self.root, "attempt-invalid-cycle", cyclic
                )
            deployment = journal.EventJournal.create_with_intent(
                self.root, "attempt-invalid-input", {"candidate": "one"}
            )
            invalid_ref = {
                "schema": journal.BLOB_REF_SCHEMA,
                "name": "missing",
                "path": "blobs/sha256/" + "0" * 64,
                "sha256": "sha256:" + "0" * 64,
                "bytes": 0,
                "media_type": "application/json",
            }
            with self.assertRaises(journal.JournalValidationError):
                deployment.append("observation", {"missing_blob": invalid_ref})
            self.assertEqual([event["kind"] for event in deployment.events], ["intent"])
        unexpected = deployment.attempt_dir / "unexpected"
        unexpected.symlink_to(deployment.attempt_dir / "events", target_is_directory=True)
        with self.assertRaisesRegex(journal.JournalValidationError, "unexpected entry"):
            journal.EventJournal.load(deployment.attempt_dir)


class DurablePublicationTest(JournalFixture):
    def test_event_publish_uses_fsync_and_fullfsync_when_available(self) -> None:
        with tempfile.TemporaryFile() as stream:
            descriptor = stream.fileno()
            with mock.patch.object(journal.os, "fsync") as fsync_mock:
                with mock.patch.object(journal.fcntl, "fcntl") as fcntl_mock:
                    journal._fsync_file(descriptor)
        fsync_mock.assert_called_once_with(descriptor)
        if getattr(journal.fcntl, "F_FULLFSYNC", None) is not None:
            fcntl_mock.assert_called_once_with(descriptor, journal.fcntl.F_FULLFSYNC)
        else:
            fcntl_mock.assert_not_called()

    def test_failed_file_sync_never_commits_an_event(self) -> None:
        with journal.PromotionLock(self.root):
            deployment = journal.EventJournal.create_with_intent(
                self.root, "attempt-sync-failure", {"candidate": "one"}
            )
            with mock.patch.object(
                journal,
                "_fsync_file",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    deployment.append("observation", {"candidate": "one"})
            reloaded = journal.EventJournal.load(deployment.attempt_dir)
            self.assertEqual([event["kind"] for event in reloaded.events], ["intent"])
            self.assertIsNotNone(reloaded.chain_tip)
            self.assertTrue(reloaded.incomplete)

    def test_failed_atomic_create_never_publishes_an_empty_attempt(self) -> None:
        with journal.PromotionLock(self.root):
            with mock.patch.object(
                journal,
                "_publish_immutable",
                side_effect=OSError("injected publication failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    journal.EventJournal.create_with_intent(
                        self.root,
                        "not-published",
                        {"candidate": "one"},
                    )
            self.assertFalse((self.root / "attempts" / "not-published").exists())
            self.assertEqual(journal.list_incomplete_attempts(self.root), [])

    def test_crash_after_atomic_rename_leaves_reconcilable_intent(self) -> None:
        class InjectedCrash(BaseException):
            pass

        real_rename = os.rename

        def rename_then_crash(source, destination):
            real_rename(source, destination)
            raise InjectedCrash

        with journal.PromotionLock(self.root):
            with mock.patch.object(journal.os, "rename", side_effect=rename_then_crash):
                with self.assertRaises(InjectedCrash):
                    journal.EventJournal.create_with_intent(
                        self.root,
                        "published-before-crash",
                        {"candidate": "one"},
                    )
            found = journal.list_incomplete_attempts(self.root)
            self.assertEqual([item.attempt_id for item in found], ["published-before-crash"])
            self.assertEqual([event["kind"] for event in found[0].events], ["intent"])


if __name__ == "__main__":
    unittest.main()
