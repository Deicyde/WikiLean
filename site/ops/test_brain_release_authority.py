#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import brain_release_authority as authority  # noqa: E402


class ReleaseNeutralPathsTest(unittest.TestCase):
    def test_neutral_paths(self):
        for path in (
            "docs/BRAIN-RELEASE-RUNBOOK.md",
            "README.md",
            "brain/README.md",
            "wiki/README.md",
            "CLAUDE.md",
            ".gitignore",
            ".github/workflows/ci.yml",
            "scripts/ci-python.sh",
            "site/ops/brain-canary.py",
            "site/ops/brain_promote_release.py",
            "site/ops/test_brain_canary.py",
            "brain/test_semantic_diff.py",
            "brain/tools/test_tool.py",
            "wiki/test/brain-api.test.ts",
            "wiki/e2e/brain.spec.ts",
            "bench/run_benchmark.py",
            "bot/poll.py",
            "manage/status.py",
            "wikifunctions/lean/Foo.lean",
            "brain/authority/fixtures/release-profile-v1-conformance.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(authority.is_release_neutral(path))

    def test_release_affecting_paths(self):
        for path in (
            "brain/build_cells.py",
            "brain/tools/semantic_diff.py",
            "brain/tools/build_release.py",
            "brain/authority/reducer-inputs-v1.json",
            "brain/authority/schemas/release/v1.json",
            "brain/authority/requests/d1-community-snapshot-v1.json",
            "catalog/data/source_registry.json",
            "site/annotations/Group_theory.json",
            "site/assets/style.css",
            "site/build_brain_page.py",
            "site/ops/nightly.env",
            "site/ops/brain-nightly.sh",
            "site/test_frontier_page.py",
            "brain/SCHEMA.md",
            "docs.md",
            "wiki/src/NOTES.md",
            "wiki/src/index.ts",
            "wiki/wrangler.jsonc",
            "wiki/package-lock.json",
            "wiki/package.json",
            "wiki/public-asset-source-attestation.json",
            "wiki/scripts/brain-release-public.ts",
            "brand-new-directory/file.txt",
            "tests.py",
            "docs",
        ):
            with self.subTest(path=path):
                self.assertFalse(authority.is_release_neutral(path))

    def test_odd_spellings_are_never_neutral(self):
        for path in ("", "/docs/x.md", "docs//x.md", "docs/../brain/x.md", "./docs/x.md", "docs/./x.md", 7):
            with self.subTest(path=path):
                self.assertFalse(authority.is_release_neutral(path))  # type: ignore[arg-type]

    def test_blocking_changes_sorts_and_dedupes(self):
        self.assertEqual(
            authority.blocking_changes(["wiki/src/b.ts", "docs/a.md", "wiki/src/a.ts", "wiki/src/b.ts"]),
            ["wiki/src/a.ts", "wiki/src/b.ts"],
        )
        self.assertEqual(authority.blocking_changes(["docs/a.md", "README.md"]), [])

    def test_changed_paths_parses_nul_separated_git_output(self):
        calls = []

        def run_git(arguments):
            calls.append(list(arguments))
            return "docs/b.md\0wiki/src/index.ts\0docs/a.md\0"

        self.assertEqual(
            authority.changed_paths(run_git, "a" * 40, "b" * 40),
            ["docs/a.md", "docs/b.md", "wiki/src/index.ts"],
        )
        self.assertEqual(
            calls,
            [["diff", "--no-renames", "--name-only", "-z", "a" * 40, "b" * 40]],
        )
        self.assertEqual(authority.changed_paths(lambda arguments: "", "a" * 40, "b" * 40), [])

    def test_validate_neutral_changes(self):
        self.assertEqual(authority.validate_neutral_changes([], "x"), [])
        self.assertEqual(
            authority.validate_neutral_changes(["docs/a.md", "docs/b.md"], "x"),
            ["docs/a.md", "docs/b.md"],
        )
        for value, message in (
            ("docs/a.md", "array"),
            (["docs/b.md", "docs/a.md"], "sorted"),
            (["docs/a.md", "docs/a.md"], "sorted"),
            ([1], "array"),
            (["docs/a.md", "wiki/src/index.ts"], "release-affecting"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    authority.validate_neutral_changes(value, "x")

    def test_describe_blocking_truncates(self):
        paths = [f"brain/file{index:03d}.py" for index in range(25)]
        text = authority.describe_blocking("a" * 40, "b" * 40, paths)
        self.assertIn("brain/file000.py", text)
        self.assertIn("(+5 more)", text)
        self.assertNotIn("brain/file024.py", text)


if __name__ == "__main__":
    unittest.main()
