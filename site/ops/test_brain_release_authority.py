#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import brain_release_authority as authority  # noqa: E402


def _nightly_watched() -> list[str]:
    """The pathspecs brain-nightly.sh's uncommitted-source check passes to git status."""
    lines = (REPO / "site/ops/brain-nightly.sh").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "status --porcelain --" in line)
    command = []
    for line in lines[start:]:
        command.append(line.rstrip().removesuffix("\\"))
        if not line.rstrip().endswith("\\"):
            break
    words = " ".join(command).split("status --porcelain --", 1)[1].split(')"', 1)[0].split()
    return [word.strip("'\"") for word in words]


class ReleaseNeutralPathsTest(unittest.TestCase):
    def test_neutral_paths(self):
        for path in (
            "docs/BRAIN-RELEASE-RUNBOOK.md",
            "README.md",
            "brain/README.md",
            "wiki/README.md",
            "AGENTS.md",
            "CLAUDE.md",
            "CONTRIBUTING.md",
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
            "bot/state/bot_state.json",
            "bot/state/batch7_approved.json",
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
            "bot/state/cut_log.json",
            "bot/state/seed_queue.json",
            "site/annotations/Group_theory.json",
            "site/assets/style.css",
            "site/build_brain_page.py",
            "site/ops/nightly.env",
            "site/ops/brain-nightly.sh",
            "site/test_frontier_page.py",
            "brain/SCHEMA.md",
            "brain/AGENTS.md",
            "wiki/src/CONTRIBUTING.md",
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

    def test_reducer_code_and_curated_inputs_are_release_affecting(self):
        # The reducer reads these exact Git bytes, so a commit that changes one must
        # never let main move past a release built before it.
        paths = set()
        for name in sorted((REPO / "brain/authority").glob("reducer-inputs-v*.json")):
            inventory = json.loads(name.read_text(encoding="utf-8"))
            paths.update(inventory["scope"])
            # Curated inputs, and any other input the inventory roots in the repository
            # (v2 and later say so with "root": "repo").
            paths.update(entry["path"] for entry in inventory["inputs"] if "path" in entry and (
                entry["class"] == "curated_git_input" or entry.get("root") == "repo"))
        self.assertIn("bot/state/cut_log.json", paths)
        for path in sorted(paths):
            with self.subTest(path=path):
                self.assertFalse(authority.is_release_neutral(path))

    def test_every_file_the_nightly_names_is_release_affecting(self):
        # Among the files the nightly's uncommitted-source check names explicitly (its
        # directories, like wiki/, also hold neutral tests and a README), one the
        # policy calls neutral is a disagreement to resolve.
        watched = [entry for entry in _nightly_watched() if entry != "$REDUCER_SCOPE"]
        self.assertEqual([entry for entry in watched if authority.is_release_neutral(entry)], [])

    def test_neutral_files_are_outside_the_nightly_source_check(self):
        # brain-nightly.sh refuses to freeze a release while a path it watches is
        # uncommitted, so no explicitly neutral file may sit inside that list.
        script = (REPO / "site/ops/brain-nightly.sh").read_text(encoding="utf-8")
        watched = _nightly_watched()
        self.assertIn("$REDUCER_SCOPE", watched)
        self.assertIn("site/ops/brain-nightly.sh", watched)
        self.assertIn('"$REPO/brain/authority/reducer-inputs-v1.json"', script)
        inventory = json.loads((REPO / "brain/authority/reducer-inputs-v1.json").read_text(encoding="utf-8"))
        watched = [entry.rstrip("/") for entry in watched if entry != "$REDUCER_SCOPE"] + inventory["scope"]
        for path in authority.NEUTRAL_FILES:
            for entry in watched:
                with self.subTest(path=path, entry=entry):
                    self.assertFalse(
                        entry in {"", "."}
                        or path == entry
                        or path.startswith(entry + "/")
                        or fnmatch.fnmatchcase(path, entry)
                    )

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
