#!/usr/bin/env python3
"""Revoke leaked GitHub tokens found in backups, files or a D1 query, without ever showing them.

    python3 -I scripts/revoke_github_tokens.py --file old-backup.sql [--file …] \\
        [--d1 wikilean --d1-sql "SELECT access_token FROM accounts WHERE …"] [--submit]

Every source is scanned as text for GitHub token formats (gho_, ghu_, ghp_, ghs_, ghr_,
github_pat_), so the tool needs no schema: a `wrangler d1 export` dump, a JSON export
and a log all work. `.gz` files are read decompressed, and `--file -` reads stdin, so an
encrypted backup never has to touch disk (`age -d -i KEY backup.sql.age | … --file -`).
`--d1` runs `npx wrangler d1 execute <db> --remote --json --command <sql>` from
`--wiki-dir` (the default is this checkout's wiki/), with wrangler's on-disk debug log
off, and scans its output.

Tokens are deduplicated and reported only as their type and the first 12 hex digits of
their SHA-256. A token is never printed, written to disk, or put on a command line.
The token `gh auth token` returns on this host is compared in memory and never revoked.

Without --submit this is a dry run. --submit posts the tokens to GitHub's
exposed-credential endpoint (POST /credentials/revoke, at most 1000 per request), which
must be called unauthenticated and allows 60 requests an hour; GitHub revokes each token
and emails its owner. A run that needs more than 60 requests refuses before sending any.

WikiLean's sign-in tokens: better-auth keeps each person's latest GitHub token in D1
`accounts` (`access_token`, `refresh_token`; `updated_at` is in seconds). Backups up to
2026-10-13 were public plaintext (docs/SECRETS.md), so the tokens to revoke are those in
any copy of them plus the rows they held that have not changed since:

    --d1 wikilean --d1-sql "SELECT access_token, refresh_token FROM accounts
                            WHERE provider_id = 'github' AND updated_at < 1791936000"

(1791936000 is 2026-10-14T00:00Z; the last plaintext backup run started 2026-10-13T16:06Z.)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"\b(gh[opusr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{82})\b")
REVOKE_URL = "https://api.github.com/credentials/revoke"
BATCH = 1000
MAX_REQUESTS = 60


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def kind(token: str) -> str:
    return "github_pat" if token.startswith("github_pat_") else token[:3]


def tokens_in(text: str) -> set[str]:
    return set(TOKEN.findall(text))


def read_file(path: Path) -> str:
    data = sys.stdin.buffer.read() if str(path) == "-" else path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def d1_output(db: str, sql: str, wiki_dir: Path) -> str:
    """The JSON a remote D1 query prints. Only the SQL rides on argv; the rows stay in a pipe."""
    result = subprocess.run(
        ["npx", "--no-install", "wrangler", "d1", "execute", db, "--remote", "--json", "--command", sql],
        cwd=wiki_dir, capture_output=True, text=True, timeout=300,
        env={**os.environ, "WRANGLER_LOG": "warn", "WRANGLER_WRITE_LOGS": "false"},
    )
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise SystemExit(f"wrangler d1 execute failed ({result.returncode}): {TOKEN.sub('[token]', tail)[:300]}")
    return result.stdout


def current_gh_token() -> str:
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def collect(files: list[Path], d1: str | None, d1_sql: str | None, wiki_dir: Path) -> dict[str, set[str]]:
    """token -> the names of the sources it appeared in."""
    found: dict[str, set[str]] = {}
    sources = [(str(p), lambda p=p: read_file(p)) for p in files]
    if d1:
        sources.append((f"d1:{d1}", lambda: d1_output(d1, d1_sql or "", wiki_dir)))
    for name, read in sources:
        for token in tokens_in(read()):
            found.setdefault(token, set()).add(name)
    return found


def revoke(tokens: list[str], opener=urllib.request.urlopen) -> list[int]:
    """POST the tokens in batches, unauthenticated. Returns each request's HTTP status."""
    statuses = []
    for start in range(0, len(tokens), BATCH):
        body = json.dumps({"credentials": tokens[start:start + BATCH]}).encode()
        request = urllib.request.Request(REVOKE_URL, data=body, method="POST", headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wikilean-revoke-github-tokens",
        })
        try:
            with opener(request, timeout=60) as response:
                statuses.append(response.status)
        except urllib.error.HTTPError as error:
            statuses.append(error.code)
    return statuses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", type=Path, action="append", default=[], help="a file to scan (.gz is decompressed; - is stdin)")
    parser.add_argument("--d1", help="a D1 database name to query with wrangler")
    parser.add_argument("--d1-sql", help="the query to run on --d1; its JSON output is scanned")
    parser.add_argument("--wiki-dir", type=Path, default=ROOT / "wiki", help="where wrangler runs")
    parser.add_argument("--submit", action="store_true", help="revoke; without it, report only")
    args = parser.parse_args(argv)
    if bool(args.d1) != bool(args.d1_sql):
        parser.error("--d1 and --d1-sql go together")
    if not args.file and not args.d1:
        parser.error("give at least one --file or --d1")
    for path in args.file:
        if str(path) != "-" and not path.is_file():
            parser.error(f"not a file: {path}")

    found = collect(args.file, args.d1, args.d1_sql, args.wiki_dir)
    own = current_gh_token()
    skipped = own in found
    found.pop(own, None)
    tokens = sorted(found, key=digest)

    for token in tokens:
        print(f"{kind(token):10} {digest(token)}  {', '.join(sorted(found[token]))}")
    counts: dict[str, int] = {}
    for token in tokens:
        counts[kind(token)] = counts.get(kind(token), 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"
    print(f"{len(tokens)} distinct tokens ({summary})"
          + ("; this host's gh login token was found and is left alone" if skipped else ""))

    if not args.submit or not tokens:
        if tokens:
            print("dry run: add --submit to revoke them")
        return 0
    requests = -(-len(tokens) // BATCH)
    if requests > MAX_REQUESTS:
        print(f"refusing: {requests} requests exceed GitHub's {MAX_REQUESTS} an hour; split the sources")
        return 1
    statuses = revoke(tokens)
    print("GitHub answered " + ", ".join(str(s) for s in statuses))
    return 0 if all(200 <= s < 300 for s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
