#!/usr/bin/env python3
"""Hermetic integration tests for lean_repo's immutable Git input path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import lean_repo  # noqa: E402


class LeanRepoGitSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "WikiLean test")
        self.git("config", "user.email", "test@wikilean.invalid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        process = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            stdin=subprocess.DEVNULL,
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

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit_all(self, message: str = "fixture") -> str:
        self.git("add", "--all")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def harvest(self) -> tuple[mock.Mock, mock.Mock, mock.Mock]:
        refresh = mock.Mock(return_value=None)
        volume_guard = mock.Mock()
        writer = mock.Mock()
        with (
            mock.patch.object(lean_repo, "ensure_checkout", refresh),
            mock.patch.object(lean_repo.common, "_volume_guard", volume_guard),
            mock.patch.object(lean_repo.common, "write_jsonl", writer),
            redirect_stderr(StringIO()),
        ):
            lean_repo.harvest_repo(
                "fixture",
                "Example",
                "Repository",
                "Lib",
                self.repo,
                self.root / "out.jsonl",
                "fixture license",
            )
        return refresh, volume_guard, writer

    def test_harvest_uses_one_commit_and_ignores_worktree_and_index(self) -> None:
        self.write(
            "Lib/A.lean",
            """/-! See https://oeis.org/A000001 -/
namespace Stable
/-- Committed documentation. -/
def value : Nat := 1
end Stable
""",
        )
        self.write("Lib/B.lean", "theorem other : True := by trivial\n")
        commit = self.commit_all()

        self.write("Lib/A.lean", "def dirty : Nat := 99\n")
        self.write("Lib/B.lean", "def staged : Nat := 98\n")
        self.git("add", "Lib/B.lean")
        self.write("Lib/Untracked.lean", "def untracked : Nat := 97\n")

        real_reader = lean_repo.git_snapshot.read_text_snapshot
        with mock.patch.object(
            lean_repo.git_snapshot,
            "read_text_snapshot",
            wraps=real_reader,
        ) as reader:
            refresh, volume_guard, writer = self.harvest()

        refresh.assert_called_once_with("Example", "Repository", self.repo)
        reader.assert_called_once_with(
            self.repo, scope="Lib", suffixes=(".lean",)
        )
        volume_guard.assert_called_once_with(
            self.root / "out.jsonl", "decl", 2
        )
        writer.assert_called_once()
        out, metadata, rows = writer.call_args.args
        self.assertEqual(out, self.root / "out.jsonl")
        self.assertEqual(metadata["commit"], commit)
        self.assertEqual(metadata["n_files"], 2)
        self.assertEqual(metadata["n_decls"], 2)
        self.assertEqual(
            [(row["decl"], row["file"]) for row in rows],
            [("Stable.value", "Lib/A.lean"), ("other", "Lib/B.lean")],
        )
        self.assertNotIn("dirty", {row["decl"] for row in rows})
        self.assertNotIn("staged", {row["decl"] for row in rows})
        self.assertNotIn("untracked", {row["decl"] for row in rows})

    def test_existing_library_without_lean_files_fails_before_write(self) -> None:
        self.write("Lib/README.md", "no Lean source here\n")
        self.commit_all()

        with (
            mock.patch.object(lean_repo, "ensure_checkout"),
            mock.patch.object(lean_repo.common, "_volume_guard") as volume_guard,
            mock.patch.object(lean_repo.common, "write_jsonl") as writer,
            self.assertRaisesRegex(RuntimeError, r"contains no \.lean files"),
        ):
            lean_repo.harvest_repo(
                "fixture",
                "Example",
                "Repository",
                "Lib",
                self.repo,
                self.root / "out.jsonl",
                "fixture license",
            )

        volume_guard.assert_not_called()
        writer.assert_not_called()

    def test_declaration_cap_still_fails_before_write(self) -> None:
        self.write(
            "Lib/Many.lean",
            "def one : Nat := 1\ndef two : Nat := 2\n",
        )
        self.commit_all()

        with (
            mock.patch.object(lean_repo, "ensure_checkout"),
            mock.patch.object(lean_repo, "DECL_CAP", 1),
            mock.patch.object(lean_repo.common, "_volume_guard") as volume_guard,
            mock.patch.object(lean_repo.common, "write_jsonl") as writer,
            self.assertRaisesRegex(RuntimeError, r">1 declarations"),
        ):
            lean_repo.harvest_repo(
                "fixture",
                "Example",
                "Repository",
                "Lib",
                self.repo,
                self.root / "out.jsonl",
                "fixture license",
            )

        volume_guard.assert_not_called()
        writer.assert_not_called()

    def test_user_repo_failure_does_not_stop_later_repository(self) -> None:
        user_data = self.root / "user-repos"
        user_data.mkdir()
        registrations = user_data / "registrations.json"
        registrations.write_text(
            json.dumps({
                "repos": [
                    {"owner": "First", "repo": "Broken", "lib": "FirstLib"},
                    {"owner": "Second", "repo": "Works", "lib": "SecondLib"},
                ]
            }),
            encoding="utf-8",
        )
        harvester = mock.Mock(side_effect=[RuntimeError("broken"), None])

        with (
            mock.patch.object(lean_repo, "REGISTRATIONS", registrations),
            mock.patch.object(lean_repo, "USER_REPOS_DIR", user_data),
            mock.patch.object(lean_repo, "preflight_public_repo"),
            mock.patch.object(lean_repo, "harvest_repo", harvester),
            mock.patch.dict(
                os.environ,
                {"BRAIN_USER_REPO_CHECKOUTS": str(self.root / "checkouts")},
            ),
            redirect_stderr(StringIO()),
        ):
            result = lean_repo.user_repos_main()

        self.assertEqual(result, 1)
        self.assertEqual(harvester.call_count, 2)
        self.assertEqual(harvester.call_args_list[1].args[1:4], (
            "Second", "Works", "SecondLib"
        ))


if __name__ == "__main__":
    unittest.main()
