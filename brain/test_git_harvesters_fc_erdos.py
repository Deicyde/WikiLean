#!/usr/bin/env python3
"""Hermetic Git-snapshot integration tests for FC and Erdős harvesters."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain" / "ingest"))
# PyYAML is an ingest-host dependency rather than part of the hermetic CI
# environment.  Supply only the import surface here; each Erdős test replaces
# ``safe_load`` with a fixture parser that also asserts the exact input bytes.
try:
    import yaml as _yaml  # type: ignore[import-not-found]  # noqa: F401
except ModuleNotFoundError:
    yaml_stub = types.ModuleType("yaml")

    def _unexpected_safe_load(_text: str) -> object:
        raise AssertionError("test must replace yaml.safe_load")

    yaml_stub.safe_load = _unexpected_safe_load  # type: ignore[attr-defined]
    sys.modules["yaml"] = yaml_stub
import erdosproblems  # noqa: E402
import formal_conjectures  # noqa: E402
import git_snapshot  # noqa: E402


class GitRepositoryFixture(unittest.TestCase):
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

    def write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit_all(self) -> str:
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "fixture")
        return self.git("rev-parse", "HEAD")


class FormalConjecturesSnapshotTests(GitRepositoryFixture):
    def test_main_binds_rows_and_metadata_to_committed_snapshot(self) -> None:
        self.write(
            "FormalConjectures/Main.lean",
            """/-! Committed module. -/
namespace FormalConjectures
/-- Committed statement; see https://oeis.org/A000001. -/
@[category research open, AMS 11]
theorem committed : True := by
  trivial
end FormalConjectures
""",
        )
        commit = self.commit_all()

        self.write(
            "FormalConjectures/Main.lean",
            "theorem staged_not_committed : True := by trivial\n",
        )
        self.git("add", "FormalConjectures/Main.lean")
        self.write(
            "FormalConjectures/Untracked.lean",
            "theorem untracked : True := by trivial\n",
        )
        output = self.root / "formal_conjectures.jsonl"

        with (
            mock.patch.object(formal_conjectures, "CHECKOUT", self.repo),
            mock.patch.object(formal_conjectures, "OUT", output),
            mock.patch.object(formal_conjectures, "ensure_checkout") as refresh,
        ):
            self.assertEqual(formal_conjectures.main(), 0)

        refresh.assert_called_once_with()
        records = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(records[0]["_meta"]["commit"], commit)
        self.assertEqual(records[0]["_meta"]["n_files"], 1)
        self.assertEqual(
            [row["decl"] for row in records[1:]],
            ["FormalConjectures.committed"],
        )
        self.assertEqual(records[1]["file"], "FormalConjectures/Main.lean")

    def test_snapshot_failure_precedes_all_output_calls(self) -> None:
        self.write("README.md", "no source root\n")
        self.commit_all()

        with (
            mock.patch.object(formal_conjectures, "CHECKOUT", self.repo),
            mock.patch.object(formal_conjectures, "ensure_checkout"),
            mock.patch.object(formal_conjectures.common, "_volume_guard") as guard,
            mock.patch.object(formal_conjectures.common, "write_jsonl") as writer,
            self.assertRaisesRegex(git_snapshot.GitSnapshotError, "scope is absent"),
        ):
            formal_conjectures.main()

        guard.assert_not_called()
        writer.assert_not_called()


class ErdosProblemsSnapshotTests(GitRepositoryFixture):
    def test_main_binds_both_outputs_to_committed_snapshot(self) -> None:
        committed_yaml = """- number: 1
  status:
    state: open
  prize: $500
  oeis: [A000001]
  tags: [number theory]
  formalized:
    state: partial
"""
        self.write(
            "data/problems.yaml",
            committed_yaml,
        )
        commit = self.commit_all()
        self.write(
            "data/problems.yaml",
            """- number: 99
  status:
    state: proved
""",
        )
        self.git("add", "data/problems.yaml")

        joins_output = self.root / "erdos_joins.jsonl"
        with (
            mock.patch.object(erdosproblems, "CHECKOUT", self.repo),
            mock.patch.object(erdosproblems, "JOINS_OUT", joins_output),
            mock.patch.object(erdosproblems, "ensure_checkout") as refresh,
            mock.patch.object(erdosproblems.common, "emit") as emit,
            mock.patch.object(
                erdosproblems.yaml,
                "safe_load",
                side_effect=lambda text: (
                    self.assertEqual(text, committed_yaml)
                    or [{
                        "number": 1,
                        "status": {"state": "open"},
                        "prize": "$500",
                        "oeis": ["A000001"],
                        "tags": ["number theory"],
                        "formalized": {"state": "partial"},
                    }]
                ),
            ),
        ):
            self.assertEqual(erdosproblems.main(), 0)

        refresh.assert_called_once_with()
        emit.assert_called_once()
        db, pages, links = emit.call_args.args
        self.assertEqual(db, "erdos")
        self.assertEqual([page["id"] for page in pages], ["1"])
        self.assertEqual(links, [])
        self.assertEqual(
            emit.call_args.kwargs["extra_meta"]["source_pin"],
            f"teorth/erdosproblems data/problems.yaml @ {commit}",
        )
        records = [json.loads(line) for line in joins_output.read_text().splitlines()]
        self.assertEqual(records[0]["_meta"]["commit"], commit)
        self.assertEqual([row["erdos"] for row in records[1:]], ["1"])

    def test_snapshot_failure_precedes_all_output_calls(self) -> None:
        self.write("README.md", "no data file\n")
        self.commit_all()

        with (
            mock.patch.object(erdosproblems, "CHECKOUT", self.repo),
            mock.patch.object(erdosproblems, "ensure_checkout"),
            mock.patch.object(erdosproblems.common, "_volume_guard") as guard,
            mock.patch.object(erdosproblems.common, "emit") as emit,
            mock.patch.object(erdosproblems.common, "write_jsonl") as writer,
            self.assertRaisesRegex(git_snapshot.GitSnapshotError, "scope is absent"),
        ):
            erdosproblems.main()

        guard.assert_not_called()
        emit.assert_not_called()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
