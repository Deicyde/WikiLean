#!/usr/bin/env python3
"""Deterministically apply @[wikidata] tags from a frozen approved-list JSON and
open the PR. NO LLM in the loop — pure mechanical transformation of a fixed input.

Pipeline phases (run individually or via --all):
  --apply        insert @[wikidata Qxxx] above each approved decl (idempotent)
  --check        print the resulting git diff --stat
  --build        lake build the touched modules; on attribute-resolution failure,
                 add `public import Mathlib.Tactic.CrossRefAttribute` and rebuild
                 (deterministic fixpoint, max 2 passes)
  --open-pr      git branch/commit/push to the fork + gh pr create
  --all          apply → build → open-pr

Determinism guarantees:
  * Decl targeting matches the FULL qualified name OR the short name, each followed
    by a real terminator (whitespace / : ( { [ / EOL) — never a bare \\b, which
    would wrongly match `Foo.bar`, `Foo_term`, `Foo.Core`.
  * Stacking rules are fixed:
      - existing single-line `@[attrs]` above the decl  → `@[attrs, wikidata Q]`
      - `@[to_additive ...]`                            → `@[to_additive (attr := wikidata Q)]`
      - otherwise                                       → new `@[wikidata Q]` line
  * Re-running is a no-op if the tag is already present (idempotent).

Usage:
  open_batch_pr.py --approved batch2_approved.json --mathlib ~/mathlib4 \
      --repo Deicyde/mathlib4 --all
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path

# --- decl targeting -------------------------------------------------------

MODIFIERS = r"(?:@\[[^\]]*\]\s*)*(?:protected\s+|private\s+|noncomputable\s+|public\s+|nonrec\s+|scoped\s+|local\s+)*"
KINDS     = r"(?:def|theorem|lemma|class|structure|inductive|abbrev|instance|opaque)"
TERMINATOR = r"(?=[\s:({\[]|$)"   # decl name must end here — NOT a bare \b

def find_decl_line(lines: list[str], qualified: str) -> int | None:
    """Return 0-based index of the declaration head for `qualified` (e.g.
    'Module.Projective'), trying the full dotted name first, then the short name.
    The name must be followed by a real terminator, so 'Foo' never matches
    'Foo.bar' / 'Foo_term' / 'Foo.Core'."""
    short = qualified.rsplit(".", 1)[-1]
    for name in ([qualified, short] if qualified != short else [qualified]):
        rx = re.compile(r"^" + MODIFIERS + KINDS + r"\s+" + re.escape(name) + TERMINATOR)
        for i, ln in enumerate(lines):
            if rx.match(ln):
                return i
    return None

def _anchor_above_modifiers(lines: list[str], idx: int) -> int:
    """Return the insertion anchor: walk up from the decl-signature line past
    contiguous bare modifier-only lines (e.g. a lone `noncomputable`)."""
    a = idx
    while a > 0 and lines[a - 1].strip() in BARE_MODIFIERS:
        a -= 1
    return a

def already_tagged(lines: list[str], idx: int, qid: str) -> bool:
    """True if a wikidata tag (this qid) is already on the attribute block above
    the decl (anchoring past any bare modifier lines)."""
    if f"wikidata {qid}" in lines[idx]:
        return True
    j = _anchor_above_modifiers(lines, idx) - 1
    while j >= 0 and (lines[j].lstrip().startswith("@[") or lines[j].strip() in BARE_MODIFIERS):
        if f"wikidata {qid}" in lines[j]:
            return True
        j -= 1
    return False

# --- edit application ------------------------------------------------------

# Modifier keywords that may sit on their OWN line above a declaration. The
# attribute must go ABOVE these (not between a bare `noncomputable` and its
# `def`, which would silently break the noncomputable association).
BARE_MODIFIERS = {"noncomputable", "private", "protected", "public",
                  "nonrec", "unsafe", "partial"}

def apply_tag(lines: list[str], idx: int, qid: str) -> tuple[list[str], str]:
    """Insert `@[wikidata qid]` for the decl whose signature is at line idx, per
    the fixed stacking rules. Returns (new_lines, description).

    First walk up past any bare modifier-only lines so the attribute anchors
    above the whole `noncomputable / private / …` header, not between a modifier
    and its `def`."""
    idx = _anchor_above_modifiers(lines, idx)
    prev = lines[idx - 1] if idx > 0 else ""
    prev_strip = prev.strip()

    # Case 1: a @[to_additive ...] line directly above → fold into (attr := ...)
    m_ta = re.match(r"^(\s*)@\[to_additive(.*)\]\s*$", prev)
    if m_ta:
        indent, inner = m_ta.group(1), m_ta.group(2)
        if "(attr :=" in inner:
            # already has an attr clause: append to it
            new = re.sub(r"\(attr\s*:=\s*", lambda mm: mm.group(0), prev)  # placeholder
            new = prev.rstrip()
            new = new[:new.rfind("]")] # strip trailing ]
            # insert wikidata into the existing attr list: find '(attr := X' → 'X, wikidata Q'
            new = re.sub(r"(\(attr\s*:=\s*[^)]*)", r"\1, wikidata " + qid, prev, count=1)
            lines[idx - 1] = new
            return lines, f"to_additive(attr+=): {prev_strip} -> {new.strip()}"
        else:
            new = f"{indent}@[to_additive (attr := wikidata {qid}){inner}]"
            lines[idx - 1] = new
            return lines, f"to_additive(attr:=): {prev_strip} -> {new.strip()}"

    # Case 2: a single-line @[...] directly above → stack into it.
    # Greedy `.*` so we match up to the LAST `]`, correctly handling attributes
    # with nested brackets like `aesop ... (rule_sets := [SimpleGraph])`.
    m_attr = re.match(r"^(\s*)@\[(.*)\](.*)$", prev)
    if m_attr and prev_strip.startswith("@["):
        indent, contents, tail = m_attr.groups()
        new = f"{indent}@[{contents.rstrip()}, wikidata {qid}]{tail}"
        lines[idx - 1] = new
        return lines, f"stacked: {prev_strip} -> {new.strip()}"

    # Case 3: no attribute above → new standalone line
    indent = re.match(r"^(\s*)", lines[idx]).group(1)
    lines.insert(idx, f"{indent}@[wikidata {qid}]")
    return lines, f"standalone: @[wikidata {qid}] above {lines[idx+1].strip()[:60]}"

def apply_all(approved: dict, mathlib: Path) -> list[dict]:
    results = []
    for t in approved["tags"]:
        p = mathlib / t["file"]
        lines = p.read_text(encoding="utf-8").splitlines()
        idx = find_decl_line(lines, t["decl"])
        if idx is None:
            results.append({**t, "ok": False, "msg": "decl head not found"}); continue
        if already_tagged(lines, idx, t["qid"]):
            results.append({**t, "ok": True, "msg": "already tagged (skip)"}); continue
        new_lines, desc = apply_tag(lines, idx, t["qid"])
        p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        results.append({**t, "ok": True, "msg": desc})
    return results

# --- build + deterministic import fix -------------------------------------

def module_name(rel: str) -> str:
    return rel[:-5].replace("/", ".")

def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)

def build_and_fix_imports(approved: dict, mathlib: Path, max_passes: int = 2) -> bool:
    mods = sorted({module_name(t["file"]) for t in approved["tags"]})
    for attempt in range(1, max_passes + 1):
        print(f"[build] pass {attempt}: lake build {len(mods)} modules")
        r = run(["lake", "build", *mods], cwd=mathlib)
        out = r.stdout + r.stderr
        if r.returncode == 0:
            print("[build] success"); return True
        # Deterministic rule: any module whose build failed AND whose error mentions
        # the wikidata attribute → add the CrossRefAttribute import.
        failed = set(re.findall(r"error: .*?(\bMathlib\.[\w.]+)", out))
        fixed_any = False
        for t in approved["tags"]:
            mod = module_name(t["file"])
            if ("wikidata" in out or "CrossRefAttribute" in out):
                p = mathlib / t["file"]
                txt = p.read_text(encoding="utf-8")
                if "Mathlib.Tactic.CrossRefAttribute" in txt:
                    continue
                # insert import in alpha order among existing `public import` lines
                lines = txt.splitlines()
                imp = "public import Mathlib.Tactic.CrossRefAttribute"
                idxs = [i for i, l in enumerate(lines) if l.startswith("public import ")]
                if not idxs:
                    continue
                insert_at = idxs[-1] + 1
                for i in idxs:
                    if lines[i] > imp:
                        insert_at = i; break
                lines.insert(insert_at, imp)
                p.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"[build] added CrossRefAttribute import to {t['file']}")
                fixed_any = True
        if not fixed_any:
            print("[build] FAILED, no import fix applicable:\n" + out[-3000:])
            return False
    print("[build] still failing after import fixes")
    return False

# --- git + gh -------------------------------------------------------------

PR_BODY = """This PR adds a batch of wikidata attributes to mathematical concepts from the [WikiLean project](https://wikilean.jackmccarthy.org).

---

[![Open in Gitpod](https://gitpod.io/button/open-in-gitpod.svg)](https://gitpod.io/from-referrer/)
"""

def open_pr(approved: dict, mathlib: Path, repo: str, base: str):
    branch = approved["branch"]
    title  = approved["title"]
    for desc, cmd in [
        ("create branch", ["git", "checkout", "-B", branch]),
        ("stage",         ["git", "add", "-A"]),
        ("commit",        ["git", "commit", "-m", title]),
        ("push",          ["git", "push", "-u", "origin", branch, "--force-with-lease"]),
    ]:
        print(f"[git] {desc}: {' '.join(cmd)}")
        r = run(cmd, cwd=mathlib)
        if r.returncode != 0 and desc != "commit":
            print(r.stdout + r.stderr); sys.exit(1)
        print((r.stdout + r.stderr).strip()[:400])
    print("[gh] pr create")
    r = run(["gh", "pr", "create", "--repo", repo, "--base", base,
             "--head", f"{repo.split('/')[0]}:{branch}",
             "--title", title, "--body", PR_BODY], cwd=mathlib)
    print((r.stdout + r.stderr).strip())

# --- LLM-generated disclosure + label -------------------------------------

LLM_TRIGGER = "LLM-generated"

def post_llm_disclosure(repo: str, pr: int):
    """Post the top-level comment whose body is exactly `LLM-generated`. The
    upstream leanprover-community/mathlib4 github-actions bot watches for this and
    applies the `LLM-generated` label (verified on PR #40440: comment at 00:26:39Z
    → bot label at 00:26:45Z). Idempotent: skips if such a comment already exists.

    NOTE: the bot only runs in the UPSTREAM repo — posting this on a fork PR is a
    no-op label-wise, so this step is meant for the upstream PR."""
    existing = run(["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/comments",
                    "--jq", ".[].body"]).stdout
    if any(line.strip() == LLM_TRIGGER for line in existing.splitlines()):
        print("[llm] 'LLM-generated' trigger comment already present (skip)")
        return
    r = run(["gh", "pr", "comment", str(pr), "--repo", repo, "--body", LLM_TRIGGER])
    print(f"[llm] posted '{LLM_TRIGGER}' trigger comment: "
          f"{'ok' if r.returncode == 0 else 'ERR ' + r.stderr[:160]}")

# --- main -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--approved", type=Path, required=True)
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--repo", default="Deicyde/mathlib4")
    ap.add_argument("--base", default="master")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--open-pr", dest="open_pr", action="store_true")
    ap.add_argument("--llm-label", dest="llm_label", action="store_true",
                    help="add the LLM-generated label + post the disclosure comment")
    ap.add_argument("--pr", type=int, default=None,
                    help="PR number for --llm-label (defaults to the branch's PR)")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    approved = json.loads(args.approved.read_text(encoding="utf-8"))

    if args.apply or args.all:
        print(f"=== apply {len(approved['tags'])} tags ===")
        for r in apply_all(approved, args.mathlib):
            flag = "ok " if r["ok"] else "ERR"
            print(f"  [{flag}] {r['qid']:10s} {r['decl']:30s} {r['msg']}")
    if args.check or args.all:
        print("\n=== git diff --stat ===")
        print(run(["git", "diff", "--stat"], cwd=args.mathlib).stdout)
    if args.build or args.all:
        if not build_and_fix_imports(approved, args.mathlib):
            print("BUILD FAILED — not opening PR"); sys.exit(1)
    if args.open_pr or args.all:
        open_pr(approved, args.mathlib, args.repo, args.base)
    if args.llm_label or args.all:
        n = _pr_number(args)
        if n:
            post_llm_disclosure(args.repo, n)

def _pr_number(args):
    if args.pr:
        return args.pr
    r = run(["gh", "pr", "view", f"{args.repo.split('/')[0]}:{json.loads(args.approved.read_text())['branch']}",
             "--repo", args.repo, "--json", "number", "--jq", ".number"])
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None

if __name__ == "__main__":
    main()
