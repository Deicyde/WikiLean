#!/usr/bin/env python3
"""Regression checks for clock-free standalone Brain input metadata."""
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_INPUTS = (
    ROOT / "catalog/data/formal_conjectures.jsonl",
    ROOT / "catalog/data/erdos_joins.jsonl",
    ROOT / "catalog/data/tauceti.jsonl",
    ROOT / "catalog/data/external/arxiv_citations.jsonl",
)
FORBIDDEN_FIELDS = frozenset({
    "built_at",
    "fetched_at",
    "n_api_calls",
    "n_entry_fetched_this_run",
    "n_resolved",
    "n_twins",
})


def first_meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        first = handle.readline()
    value = json.loads(first)
    if not isinstance(value, dict) or set(value) != {"_meta"} \
            or not isinstance(value["_meta"], dict):
        raise AssertionError(f"{path}: first row is not a metadata envelope")
    return value["_meta"]


class NormalizedInputMetadataTest(unittest.TestCase):
    def test_checked_in_standalone_inputs_exclude_run_metadata(self) -> None:
        paths = [*REQUIRED_INPUTS]
        user_repos = ROOT / "catalog/data/user_repos"
        if user_repos.is_dir():
            paths.extend(sorted(user_repos.glob("*.jsonl")))
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file())
                self.assertFalse(FORBIDDEN_FIELDS.intersection(first_meta(path)))


if __name__ == "__main__":
    unittest.main()
