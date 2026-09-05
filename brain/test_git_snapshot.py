#!/usr/bin/env python3
"""Hermetic tests for brain/ingest/git_snapshot.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import git_snapshot  # noqa: E402


class GitSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "WikiLean test")
        self.git("config", "user.email", "test@wikilean.invalid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str, data: bytes | None = None) -> str:
        process = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            self.fail(
                f"git {' '.join(arguments)} failed: "
                f"{process.stderr.decode('utf-8', 'replace')}"
            )
        return process.stdout.decode("ascii", "strict").strip()

    def write(self, relative: str, content: str | bytes) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)

    def commit_all(self, message: str = "fixture") -> str:
        self.git("add", "--all")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_reads_one_commit_not_worktree_index_or_untracked_files(self) -> None:
        self.write("Lib/B.lean", "committed B\n")
        self.write("Lib/A.lean", "committed A\n")
        self.write("Lib/C.lean", "committed C\n")
        self.write("Lib/README.md", "ignored regular file\n")
        self.write("data/problems.yaml", "problems: committed\n")
        commit = self.commit_all()

        self.write("Lib/A.lean", "dirty A\n")
        self.write("Lib/B.lean", "staged B\n")
        self.git("add", "Lib/B.lean")
        (self.repo / "Lib/C.lean").unlink()
        self.write("Lib/Untracked.lean", "untracked\n")

        real_popen = subprocess.Popen
        with mock.patch.object(
            git_snapshot.subprocess, "Popen", wraps=real_popen
        ) as popen:
            snapshot = git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

        self.assertEqual(snapshot.commit, commit)
        self.assertEqual(
            [(item.path, item.text) for item in snapshot.files],
            [
                ("Lib/A.lean", "committed A\n"),
                ("Lib/B.lean", "committed B\n"),
                ("Lib/C.lean", "committed C\n"),
            ],
        )
        cat_file_calls = [
            call
            for call in popen.call_args_list
            if "cat-file" in call.args[0]
        ]
        self.assertEqual(len(cat_file_calls), 1)
        self.assertIn("--no-replace-objects", cat_file_calls[0].args[0])
        self.assertEqual(
            cat_file_calls[0].kwargs["env"]["GIT_NO_LAZY_FETCH"], "1"
        )

        exact = git_snapshot.read_text_snapshot(
            self.repo, scope="data/problems.yaml"
        )
        self.assertEqual(exact.commit, commit)
        self.assertEqual(
            exact.files,
            (git_snapshot.GitTextFile("data/problems.yaml", "problems: committed\n"),),
        )

    def test_head_movement_after_capture_does_not_change_snapshot(self) -> None:
        self.write("Lib/A.lean", "first\n")
        first = self.commit_all("first")
        original_run_git = git_snapshot._run_git
        moved = False

        def move_after_capture(
            git: str,
            repository: Path,
            arguments: tuple[str, ...] | list[str],
            *,
            allow_exit_one: bool = False,
        ) -> bytes:
            nonlocal moved
            result = original_run_git(
                git,
                repository,
                arguments,
                allow_exit_one=allow_exit_one,
            )
            if list(arguments) == ["rev-parse", "--verify", "HEAD^{commit}"]:
                self.assertFalse(moved)
                moved = True
                self.write("Lib/A.lean", "second\n")
                self.commit_all("second")
            return result

        with mock.patch.object(git_snapshot, "_run_git", side_effect=move_after_capture):
            snapshot = git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

        self.assertTrue(moved)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), first)
        self.assertEqual(snapshot.commit, first)
        self.assertEqual(snapshot.files[0].text, "first\n")

    def test_accepts_executable_blob_and_empty_suffix_selection(self) -> None:
        self.write("Lib/Run.lean", "def run := true\n")
        self.write("Docs/README.md", "documentation\n")
        self.git("add", "--all")
        self.git("update-index", "--chmod=+x", "Lib/Run.lean")
        self.git("commit", "-q", "-m", "executable")
        commit = self.git("rev-parse", "HEAD")

        executable = git_snapshot.read_text_snapshot(
            self.repo, scope="Lib", suffixes=(".lean",)
        )
        self.assertEqual(executable.commit, commit)
        self.assertEqual(
            executable.files,
            (git_snapshot.GitTextFile("Lib/Run.lean", "def run := true\n"),),
        )
        no_matches = git_snapshot.read_text_snapshot(
            self.repo, scope="Docs", suffixes=(".lean",)
        )
        self.assertEqual(no_matches.commit, commit)
        self.assertEqual(no_matches.files, ())

    def test_replacement_refs_and_inherited_git_selectors_are_ignored(self) -> None:
        self.write("Lib/A.lean", "original\n")
        original = self.commit_all("original")
        self.write("Lib/A.lean", "replacement\n")
        replacement = self.commit_all("replacement")
        self.git("checkout", "-q", "--detach", original)
        self.git("replace", original, replacement)

        other = Path(self.temporary.name) / "other"
        other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        selectors = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
            "GIT_INDEX_FILE": str(other / "host-index"),
            "GIT_OBJECT_DIRECTORY": str(other / "missing-objects"),
            "GIT_NO_REPLACE_OBJECTS": "0",
        }
        with mock.patch.dict(os.environ, selectors, clear=False):
            snapshot = git_snapshot.read_text_snapshot(
                self.repo, scope="Lib/A.lean"
            )

        self.assertEqual(snapshot.commit, original)
        self.assertEqual(snapshot.files[0].text, "original\n")

    def test_rejects_promisor_repository(self) -> None:
        self.write("Lib/A.lean", "text\n")
        self.commit_all()
        self.git("config", "remote.origin.promisor", "true")
        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "partial-clone"):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

    def test_rejects_symlink_anywhere_in_directory_scope(self) -> None:
        self.write("Lib/A.lean", "text\n")
        self.git("add", "Lib/A.lean")
        target_oid = self.git("hash-object", "-w", "--stdin", data=b"A.lean")
        self.git(
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            target_oid,
            "Lib/link.txt",
        )
        self.git("commit", "-q", "-m", "symlink")

        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "not a regular blob"):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

    def test_rejects_gitlink_anywhere_in_directory_scope(self) -> None:
        self.write("Lib/A.lean", "text\n")
        base = self.commit_all("base")
        self.git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            base,
            "Lib/vendor",
        )
        self.git("commit", "-q", "-m", "gitlink")

        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "not a regular blob"):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

    def test_rejects_non_utf8_blob(self) -> None:
        self.write("Lib/A.lean", b"valid prefix\n\xff\n")
        self.commit_all()
        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "not UTF-8"):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

    def test_rejects_missing_blob_object_and_missing_scope(self) -> None:
        self.write("Lib/A.lean", "text\n")
        self.commit_all()
        oid = self.git("rev-parse", "HEAD:Lib/A.lean")
        (self.repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()

        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "unavailable"):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )
        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "absent"):
            git_snapshot.read_text_snapshot(self.repo, scope="missing.yaml")

    def test_rejects_corrupt_blob_object(self) -> None:
        self.write("Lib/A.lean", "text\n")
        self.commit_all()
        oid = self.git("rev-parse", "HEAD:Lib/A.lean")
        object_path = self.repo / ".git" / "objects" / oid[:2] / oid[2:]
        object_path.chmod(0o600)
        object_path.write_bytes(b"not a valid loose Git object")

        with self.assertRaises(git_snapshot.GitSnapshotError):
            git_snapshot.read_text_snapshot(
                self.repo, scope="Lib", suffixes=(".lean",)
            )

    def test_requires_repository_top_level_and_normalized_scope(self) -> None:
        self.write("Lib/A.lean", "text\n")
        self.commit_all()
        with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "top level"):
            git_snapshot.read_text_snapshot(
                self.repo / "Lib", scope="Lib", suffixes=(".lean",)
            )
        for scope in (
            "/Lib",
            "Lib/../Lib",
            "./Lib",
            "Lib//A.lean",
            "Lib/bad\nname",
            "Lib/Cafe\u0301",
        ):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "normalized"):
                    git_snapshot.read_text_snapshot(
                        self.repo, scope=scope, suffixes=(".lean",)
                    )

    def test_rejects_file_count_and_blob_size_over_limits(self) -> None:
        self.write("Lib/A.lean", "abc\n")
        self.write("Lib/B.lean", "def\n")
        self.commit_all()
        with mock.patch.object(git_snapshot, "MAX_FILES", 1):
            with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "file limit"):
                git_snapshot.read_text_snapshot(
                    self.repo, scope="Lib", suffixes=(".lean",)
                )
        with mock.patch.object(git_snapshot, "MAX_BLOB_BYTES", 2):
            with self.assertRaisesRegex(git_snapshot.GitSnapshotError, "blob exceeds"):
                git_snapshot.read_text_snapshot(
                    self.repo, scope="Lib", suffixes=(".lean",)
                )


if __name__ == "__main__":
    unittest.main()
