#!/usr/bin/env python3
"""Bearer tokens never ride on a process argv, and the D1 backup job installs Wrangler alone.

A process's argv is visible to every user on the host (ps, /proc/<pid>/cmdline), so
an Authorization header must reach curl on stdin (-H @-) or from a private file,
never as an -H argument. The Backup D1 workflow must install only the pinned
.github/backup-wrangler tree, which must stay the exact Wrangler closure of
wiki/package-lock.json.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import backup_wrangler_lock  # noqa: E402

# An Authorization header passed as a curl argument, in shell/YAML or a Python argv list.
ARGV_HEADER = re.compile(
    r"""(?:-H|--header)\s*(?:=|\s)\s*["']?Authorization"""      # -H "Authorization: …"
    r"""|["'](?:-H|--header)["']\s*,\s*f?["']Authorization""",   # "-H", "Authorization: …"
    re.IGNORECASE,
)
SCANNED_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".mjs", ".js", ".ts", ".md")


def tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *patterns],
                         check=True, capture_output=True, text=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


class NoBearerOnArgv(unittest.TestCase):
    def test_pattern_catches_the_old_forms(self):
        for bad in ('-H "Authorization: Bearer $PIPELINE_TOKEN"',
                    "--header 'Authorization: Bearer x'",
                    '"-H", "Authorization: Bearer " + token',
                    '"-H", f"Authorization: Bearer {token}"'):
            self.assertRegex(bad, ARGV_HEADER)
        for good in ("-H @-", '"-H", "@-"', 'headers={"Authorization": f"Bearer {token}"}'):
            self.assertNotRegex(good, ARGV_HEADER)

    def test_no_tracked_script_workflow_or_doc_passes_a_bearer_as_an_argument(self):
        files = [p for p in tracked("*")
                 if p.suffix in SCANNED_SUFFIXES and p.is_file()
                 and "node_modules" not in p.parts and not p.name.startswith("test_")
                 and not p.name.endswith((".test.ts", ".spec.ts")) and p != Path(__file__).resolve()]
        self.assertGreater(len(files), 100)
        hits = []
        for path in files:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if ARGV_HEADER.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(hits, [])


class BackupWorkflowWrangler(unittest.TestCase):
    WORKFLOW = ROOT / ".github" / "workflows" / "backup-d1.yml"

    def test_backup_lock_is_the_wiki_wrangler_closure(self):
        self.assertEqual(backup_wrangler_lock.main(["--check"]), 0)

    def test_backup_installs_and_runs_only_the_pinned_wrangler(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("npm ci --prefix wiki", text)
        self.assertNotIn("wiki/node_modules", text)
        self.assertIn("npm ci --prefix .github/backup-wrangler --ignore-scripts", text)
        self.assertIn(".github/backup-wrangler/node_modules/.bin/wrangler d1 export", text)
        self.assertIn("cache-dependency-path: .github/backup-wrangler/package-lock.json", text)
        self.assertIn("| curl --fail -sS --max-time 300 -H @-", text)

    def test_check_fails_when_the_wiki_lock_moves_wrangler(self):
        import json
        wiki = json.loads(backup_wrangler_lock.WIKI_LOCK.read_text(encoding="utf-8"))
        manifest = json.loads((backup_wrangler_lock.TARGET / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((backup_wrangler_lock.TARGET / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(backup_wrangler_lock.problems(wiki, manifest, lock), [])
        moved = json.loads(json.dumps(wiki))
        moved["packages"]["node_modules/wrangler"]["version"] = "4.999.0"
        moved["packages"]["node_modules/wrangler"]["integrity"] = "sha512-moved"
        found = backup_wrangler_lock.problems(moved, manifest, lock)
        self.assertTrue(any("package.json differs" in p for p in found), found)
        self.assertTrue(any(p.startswith("node_modules/wrangler:") for p in found), found)


if __name__ == "__main__":
    unittest.main()
