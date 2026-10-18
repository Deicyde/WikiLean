#!/usr/bin/env python3
"""Bearer tokens never ride on a process argv, and the D1 backup job installs Wrangler alone.

A process's argv is visible to every user on the host (ps, /proc/<pid>/cmdline), so
an Authorization header must reach curl on stdin (-H @-) or from a private file,
never as an -H argument, and no Python tool may take a credential as a command-line
flag (--token, --secret, …). The scan joins shell continuations and walks Python
argv lists in any layout. The Backup D1 workflow must install only the pinned
.github/backup-wrangler tree, which must stay the exact Wrangler closure of
wiki/package-lock.json.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import backup_wrangler_lock  # noqa: E402

# An Authorization header passed as a curl argument, in shell/YAML or a Python argv list.
# Applied to whole files after backslash continuations are joined, so \s also spans
# the line breaks of a one-element-per-line argv list.
ARGV_HEADER = re.compile(
    r"""(?:-H|--header)\s*(?:=|\s)\s*["']?Authorization"""      # -H "Authorization: …"
    r"""|["'](?:-H|--header)["']\s*,\s*f?["'`]Authorization""",  # "-H", "Authorization: …"
    re.IGNORECASE,
)
SCANNED_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".mjs", ".js", ".ts", ".md")
HEADER_FLAGS = {"-H", "--header"}
AUTH_TEXT = re.compile(r"\s*Authorization\s*:", re.IGNORECASE)
INLINE_AUTH_FLAG = re.compile(r"(?:-H|--header=?)\s*Authorization\s*:", re.IGNORECASE)
# A command-line flag whose value is a credential (argparse add_argument).
SECRET_FLAG = re.compile(r"^--(?:[a-z0-9]+-)*(?:token|secret|password|passwd|api-?key|bearer)$", re.IGNORECASE)


def join_continuations(text: str) -> tuple[str, list[int]]:
    """Join shell backslash continuations; return the text and, per output
    line, the source line it starts on."""
    out: list[str] = []
    starts: list[int] = []
    pending: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not pending:
            start = number
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending.append(stripped[:-1])
            continue
        pending.append(line)
        out.append(" ".join(pending))
        starts.append(start)
        pending = []
    if pending:
        out.append(" ".join(pending))
        starts.append(start)
    return "\n".join(out), starts


def text_hits(text: str) -> list[int]:
    """Source line numbers of argv Authorization headers in shell, YAML,
    Markdown, JS/TS or Python text (continuations and multi-line lists included)."""
    joined, starts = join_continuations(text)
    hits = []
    for match in ARGV_HEADER.finditer(joined):
        hits.append(starts[joined.count("\n", 0, match.start())])
    return sorted(set(hits))


def _leading_text(node: ast.AST) -> str | None:
    """The literal text a Python string expression starts with."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
    if isinstance(node, ast.BinOp):
        return _leading_text(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _leading_text(node.func.value)
    return None


def python_hits(text: str) -> list[int]:
    """Line numbers where Python code puts an Authorization header in an argv
    list (any layout), or declares a credential-valued command-line flag."""
    tree = ast.parse(text)
    auth_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and AUTH_TEXT.match(_leading_text(node.value) or "x"):
            auth_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
            for flag, value in zip(elts, elts[1:]):
                if _leading_text(flag) not in HEADER_FLAGS:
                    continue
                lead = _leading_text(value)
                if (lead is not None and AUTH_TEXT.match(lead)) or \
                        (isinstance(value, ast.Name) and value.id in auth_names):
                    hits.append(value.lineno)
            for elt in elts:
                lead = _leading_text(elt)
                if lead and INLINE_AUTH_FLAG.match(lead):
                    hits.append(elt.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "add_argument":
            for arg in node.args:
                name = _leading_text(arg)
                if name and SECRET_FLAG.match(name):
                    hits.append(arg.lineno)
    return sorted(set(hits))


def tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *patterns],
                         check=True, capture_output=True, text=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


class NoBearerOnArgv(unittest.TestCase):
    def test_pattern_catches_the_old_forms(self):
        for bad in ('-H "Authorization: Bearer $PIPELINE_TOKEN"',
                    "--header 'Authorization: Bearer x'",
                    '"-H", "Authorization: Bearer " + token',
                    '"-H", f"Authorization: Bearer {token}"',
                    # shell continuation between the flag and the header
                    'curl -X POST -H \\\n  "Authorization: Bearer $PIPELINE_TOKEN" \\\n  "$URL"',
                    # one element per line (black/ruff magic trailing comma), Python or JS
                    'subprocess.run([\n    "curl",\n    "-H",\n    f"Authorization: Bearer {token()}",\n])',
                    'spawn("curl", [\n  "-H",\n  `Authorization: Bearer ${token}`,\n]);'):
            self.assertTrue(text_hits(bad), bad)
        for good in ("-H @-", '"-H", "@-"', 'headers={"Authorization": f"Bearer {token}"}',
                     'curl -sS -X POST "$URL" \\\n  -H @- \\\n  --data-binary @body.json'):
            self.assertEqual(text_hits(good), [], good)

    def test_python_ast_catches_any_argv_layout_and_secret_flags(self):
        bad = (
            "import subprocess\n"
            "subprocess.run([\n"
            "    'curl',\n"
            "    '-H',\n"
            "    f'Authorization: Bearer {token()}',\n"   # line 5
            "    url,\n"
            "])\n"
            "hdr = 'Authorization: Bearer %s' % tok\n"
            "argv = ('curl', '--header',\n"
            "        hdr)\n"                               # line 10
            "argv2 = ['curl', '--header=Authorization: Bearer ' + tok]\n"  # line 11
            "argv3 = ['curl', '-H', 'Authorization: Bearer {}'.format(tok)]\n"  # line 12
            "ap.add_argument('--token', default='')\n"   # line 13
            "ap.add_argument('--api-key')\n"             # line 14
        )
        self.assertEqual(python_hits(bad), [5, 10, 11, 12, 13, 14])
        good = (
            "subprocess.run(['curl', '-H', '@-', url], input=f'Authorization: Bearer {t}\\n')\n"
            "requests.get(url, headers={'Authorization': f'Bearer {t}'})\n"
            "ap.add_argument('--budget-tokens', type=int)\n"
            "ap.add_argument('--i-understand-this-costs-tokens', action='store_true')\n"
        )
        self.assertEqual(python_hits(good), [])

    def test_no_tracked_script_workflow_or_doc_passes_a_bearer_as_an_argument(self):
        files = [p for p in tracked("*")
                 if p.suffix in SCANNED_SUFFIXES and p.is_file()
                 and "node_modules" not in p.parts and not p.name.startswith("test_")
                 and not p.name.endswith((".test.ts", ".spec.ts")) and p != Path(__file__).resolve()]
        self.assertGreater(len(files), 100)
        hits = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            numbers = text_hits(text)
            if path.suffix == ".py":
                numbers = sorted(set(numbers) | set(python_hits(text)))
            hits += [f"{path.relative_to(ROOT)}:{n}: {lines[n - 1].strip()}" for n in numbers]
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
