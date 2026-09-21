#!/usr/bin/env python3
"""Tests for the non-deterministic triage process boundary."""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

import triage


class TriageEnvironmentTest(unittest.TestCase):
    def test_claude_child_receives_only_explicit_runtime_and_auth_values(self):
        child = subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout='{"result":"{\\"decision\\":\\"cut\\"}"}',
            stderr="",
        )
        entry = {
            "qid": "Q1",
            "decl": "Fixture.decl",
            "file": "Fixture.lean",
            "verdicts": "changes requested",
            "notes": [],
        }
        ambient = {
            "PATH": "/tools",
            "HOME": "/home/runner",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
            "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "default-github-secret",
            "WIKILEAN_API_TOKEN": "wiki-secret",
            "QUICKSTATEMENTS_TOKEN": "wikidata-secret",
            "UNRELATED_SECRET": "ambient-secret",
        }

        with mock.patch.dict(os.environ, ambient, clear=True), mock.patch.object(
            triage.subprocess,
            "run",
            return_value=child,
        ) as run:
            result = triage.ask_llm(entry, "")

        self.assertEqual(result["decision"], "cut")
        command = run.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            environment,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
                "HOME": "/home/runner",
                "PATH": "/tools",
            },
        )


if __name__ == "__main__":
    unittest.main()
