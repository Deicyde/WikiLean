#!/usr/bin/env python3
"""Generic Lean-repo frontier harvester (TauCeti + user-registered repos).

Parameterizes brain/ingest/formal_conjectures.py's harvest for an ARBITRARY
public GitHub Lean repo: clone/pull a mirror checkout (outside WikiLean — never
inside the repo), capture <Lib>/**/*.lean from one immutable Git commit, and
emit one row per declaration
(FQ name, module, decl kind, the @[category ..., AMS ...] attribute when
present, /-- docstring -/, a statement-header code snippet, and every
erdos/wikipedia/oeis reference URL the docstrings cite) — the exact row shape
formal_conjectures.jsonl uses, so build_common's frontier-repo layer mints
decl:<Lib>:* nodes from these files identically (fail-soft: file missing =
that repo's layer skipped).

Deterministic, no LLM. Reuses common.py plumbing (_meta first line, atomic
write, _volume_guard, loud 0-decl failure; BRAIN_INGEST_FORCE=1 overrides the
volume floor) and formal_conjectures.py's Lean parsing helpers.

Named specs (catalog/data/<key>.jsonl, committed):
    python3 brain/ingest/lean_repo.py tauceti
User-registered repos (catalog/data/user_repos/registrations.json, synced
nightly from the Worker's GET /api/repos/enabled; file missing = fail-soft
skip; outputs catalog/data/user_repos/<owner>__<repo>.jsonl):
    python3 brain/ingest/lean_repo.py --user-repos

Env: BRAIN_TC_CHECKOUT         (default /Users/jack/Desktop/LEAN/tauceti-mirror)
     BRAIN_USER_REPO_CHECKOUTS (default /Users/jack/Desktop/LEAN/user-lean-repos)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common                     # noqa: E402  (brain/ingest/common.py)
import build_common               # noqa: E402  (brain/build_common.py — Lean parser)
import formal_conjectures as fc   # noqa: E402  (shared decl-context helpers)
import git_snapshot               # noqa: E402  (immutable committed source bytes)

# GitHub owner/repo segments reach `git clone` and the filesystem, so REJECT
# anything outside this strict allowlist before any subprocess/path use. The
# charset has no '/', '\\' or whitespace (no path separators, no option
# smuggling — and every constructed URL starts with "https://", never the
# segment itself); the owner must not start with '.' and neither segment may
# be a bare dot-path.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}\Z")
# Lean library name: the source root dir inside the checkout AND the
# decl:<Lib>: node-id prefix (pinned cross-agent contract with /api/repos).
LIB_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}\Z")

DECL_CAP = 20_000        # per-repo hard cap — fail loud, never truncate silently
USER_REPO_CAP = 50       # global cap on user-registered repos per harvest run

USER_REPOS_DIR = common.REPO / "catalog" / "data" / "user_repos"
REGISTRATIONS = USER_REPOS_DIR / "registrations.json"

SPECS: dict[str, dict] = {
    "tauceti": {
        "owner": "TauCetiProject", "repo": "TauCeti", "lib": "TauCeti",
        "checkout": Path(os.environ.get(
            "BRAIN_TC_CHECKOUT", "/Users/jack/Desktop/LEAN/tauceti-mirror")),
        "out": common.REPO / "catalog" / "data" / "tauceti.jsonl",
        "license": "Apache-2.0 (TauCeti contributors, TauCetiProject/TauCeti) "
                   "— docstrings/code stored with attribution",
    },
}


def validate_owner_repo(owner: str, repo: str) -> None:
    """Reject anything unsafe to hand to git or the filesystem — see NAME_RE."""
    for label, part in (("owner", owner), ("repo", repo)):
        if (not isinstance(part, str) or not NAME_RE.match(part)
                or part in (".", "..")):
            raise ValueError(f"invalid {label} {part!r} "
                             f"(must match {NAME_RE.pattern})")
    if owner.startswith("."):
        raise ValueError(f"invalid owner {owner!r} (must not start with '.')")


def default_lib(repo: str) -> str:
    """repo name -> Lean library name (the /api/repos contract default):
    CamelCase the alphanumeric runs, strip everything else
    ('my-lean_lib' -> 'MyLeanLib'). May still fail LIB_RE (e.g. a leading
    digit) — callers must validate."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", repo) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


REPO_SIZE_CAP_KB = 200 * 1024  # GitHub API `size` is in KB


def preflight_public_repo(owner: str, repo: str) -> None:
    """User-registered repos only: verify the repo exists, is public, and is
    under the size cap BEFORE any clone lands on this machine. Raises on
    reject; network failure also raises (no clone without a verdict — a repo
    already mirrored keeps working via ensure_checkout's fail-soft pull)."""
    import json as _json
    import ssl
    import urllib.request
    # The macOS framework Python ships no CA bundle — use certifi when present
    # (same workaround as manage/halo.py).
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "wikilean-lean-repo-ingest"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        meta = _json.load(resp)
    if meta.get("private"):
        raise ValueError(f"{owner}/{repo} is private")
    size_kb = int(meta.get("size") or 0)
    if size_kb > REPO_SIZE_CAP_KB:
        raise ValueError(f"{owner}/{repo} is {size_kb // 1024} MB "
                         f"(cap {REPO_SIZE_CAP_KB // 1024} MB)")


def ensure_checkout(owner: str, repo: str, checkout: Path) -> None:
    """Clone or (at most daily) ff-pull the mirror.

    Fail-soft on network: an existing checkout is always usable as-is.  The
    exact commit pin is captured later, together with its source bytes, by
    ``git_snapshot.read_text_snapshot``.
    """
    validate_owner_repo(owner, repo)
    url = f"https://github.com/{owner}/{repo}.git"
    if not (checkout / ".git").exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", url, str(checkout)],
                       check=True)
    else:
        stamp = checkout / ".git" / "FETCH_HEAD"
        if not stamp.exists() or time.time() - stamp.stat().st_mtime > 20 * 3600:
            r = subprocess.run(["git", "-C", str(checkout), "pull", "--ff-only"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[lean_repo:{owner}/{repo}] pull failed (using existing "
                      f"checkout): {r.stderr.strip()[:200]}", file=sys.stderr)


def harvest_rows(snapshot: git_snapshot.GitTextSnapshot,
                 lib: str) -> tuple[list[dict], int]:
    """Parse every declaration from one captured ``<lib>/**/*.lean`` snapshot.

    The row shape and declaration-context parsing match
    formal_conjectures.py's main loop.  Empty source selections are rejected
    before any caller can write a normalized input.
    """
    if not snapshot.files:
        raise RuntimeError(
            f"{lib}: captured commit {snapshot.commit} contains no .lean files "
            f"under {lib}/"
        )
    rows: list[dict] = []
    for source in snapshot.files:
        rel = source.path
        lines = source.text.splitlines()
        module = rel[:-len(".lean")].replace("/", ".")
        header = fc._module_docstring(lines)
        file_refs = fc._refs(header)
        declared = build_common._lean_decl_lines(lines)
        for fq in sorted(declared):
            idx = declared[fq]
            attr, doc = fc._decl_context(lines, idx)
            cm = fc.CATEGORY.search(attr)
            category = cm.group(1).strip() if cm else None
            ams = [int(x) for x in (cm.group(2) or "").split()] if cm else []
            kw = fc._KW.search(re.sub(r"@\[[^\]]*\]", "", lines[idx]))
            row = {
                "decl": fq, "module": module, "file": rel,
                "kind": kw.group(1) if kw else None,
                "category": category, "ams": ams or None,
                "docstring": common.clean_snippet(doc, fc.DOCSTRING_MAX) or None,
                "code": fc._code_snippet(lines, idx) or None,
                "refs": fc._refs(doc) or None,
                "file_refs": file_refs or None,
            }
            rows.append({k: v for k, v in row.items() if v is not None})
        if len(rows) > DECL_CAP:
            raise RuntimeError(
                f"{lib}: >{DECL_CAP} declarations — refusing to harvest past "
                f"the per-repo cap (a deliberate cap raise beats a silent "
                f"truncation)")
    return rows, len(snapshot.files)


def harvest_repo(key: str, owner: str, repo: str, lib: str,
                 checkout: Path, out: Path, license_note: str) -> None:
    ensure_checkout(owner, repo, checkout)
    snapshot = git_snapshot.read_text_snapshot(
        checkout, scope=lib, suffixes=(".lean",)
    )
    rows, n_files = harvest_rows(snapshot, lib)
    if not rows:
        raise RuntimeError(f"{owner}/{repo}: harvested 0 declarations — "
                           f"refusing to write (fail-soft)")
    common._volume_guard(out, "decl", len(rows))
    common.write_jsonl(out, {
        "source": f"{owner}/{repo}",
        "repo": f"{owner}/{repo}",
        "lib": lib,
        "license": license_note,
        "commit": snapshot.commit,
        "n_files": n_files,
        "n_decls": len(rows),
    }, rows)
    print(f"[lean_repo:{key}] wrote {len(rows)} decls from {n_files} files "
          f"@ {snapshot.commit[:12]} -> {out}", file=sys.stderr)


def user_repos_main() -> int:
    """Harvest every enabled user-registered repo (registrations.json, synced
    nightly from GET /api/repos/enabled). One bad repo never sinks the run;
    harvests for repos no longer registered are pruned (the build mints every
    catalog/data/user_repos/*.jsonl, so a disabled repo must lose its file)."""
    if not REGISTRATIONS.exists():
        print(f"[lean_repo] {REGISTRATIONS} missing — user-repo harvest "
              f"skipped (nightly sync not run / no registrations yet)",
              file=sys.stderr)
        return 0
    try:
        repos = json.loads(REGISTRATIONS.read_text())["repos"]
        if not isinstance(repos, list):
            raise ValueError("'repos' is not a list")
    except Exception as e:  # noqa: BLE001 — keep previous harvests on bad sync
        print(f"[lean_repo] unreadable registrations.json ({e}) — keeping "
              f"previous harvests", file=sys.stderr)
        return 1
    base = Path(os.environ.get("BRAIN_USER_REPO_CHECKOUTS",
                               "/Users/jack/Desktop/LEAN/user-lean-repos"))
    seen: set[tuple[str, str]] = set()
    valid: list[tuple[str, str, str]] = []
    n_bad = 0
    for r in repos:
        if not isinstance(r, dict):
            n_bad += 1
            print(f"[lean_repo] registration rejected: non-object row "
                  f"{str(r)[:80]!r}", file=sys.stderr)
            continue
        owner, repo = r.get("owner"), r.get("repo")
        try:
            validate_owner_repo(owner, repo)
        except ValueError as e:
            n_bad += 1
            print(f"[lean_repo] registration rejected: {e}", file=sys.stderr)
            continue
        lib = r.get("lib") or default_lib(repo)
        if not isinstance(lib, str) or not LIB_RE.match(lib):
            n_bad += 1
            print(f"[lean_repo] registration rejected: bad lib {lib!r} for "
                  f"{owner}/{repo}", file=sys.stderr)
            continue
        if (owner, repo) in seen:
            continue
        seen.add((owner, repo))
        valid.append((owner, repo, lib))
    if len(valid) > USER_REPO_CAP:
        print(f"[lean_repo] {len(valid)} registrations exceed the "
              f"{USER_REPO_CAP}-repo cap — harvesting the first "
              f"{USER_REPO_CAP} only", file=sys.stderr)
        valid = valid[:USER_REPO_CAP]
    n_fail = 0
    kept: set[str] = set()
    for owner, repo, lib in valid:
        out = USER_REPOS_DIR / f"{owner}__{repo}.jsonl"
        kept.add(out.name)
        try:
            if not (base / f"{owner}__{repo}" / ".git").exists():
                preflight_public_repo(owner, repo)
            harvest_repo(f"{owner}/{repo}", owner, repo, lib,
                         base / f"{owner}__{repo}", out,
                         "per-repo (owner-registered public GitHub repo) — "
                         "docstrings/code stored with repo attribution")
        except Exception as e:  # noqa: BLE001 — one bad repo, loud + continue
            n_fail += 1
            print(f"[lean_repo] {owner}/{repo} harvest FAILED (previous file, "
                  f"if any, intact): {e}", file=sys.stderr)
    for stale in sorted(USER_REPOS_DIR.glob("*.jsonl")):
        if stale.name not in kept:
            stale.unlink()
            print(f"[lean_repo] pruned {stale.name} (no longer "
                  f"registered/enabled)", file=sys.stderr)
    print(f"[lean_repo] user repos: {len(valid)} harvested targets, "
          f"{n_fail} failed, {n_bad} rejected", file=sys.stderr)
    return 1 if n_fail else 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--user-repos":
        return user_repos_main()
    if len(argv) == 2 and argv[1] in SPECS:
        s = SPECS[argv[1]]
        harvest_repo(argv[1], s["owner"], s["repo"], s["lib"],
                     s["checkout"], s["out"], s["license"])
        return 0
    print(f"usage: lean_repo.py {{{'|'.join(sorted(SPECS))}}} | --user-repos",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
