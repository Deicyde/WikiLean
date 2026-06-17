#!/usr/bin/env python3
"""Split a batch PR down to its GREEN tags — FULLY DETERMINISTIC (no LLM).

Given the recycle qids, remove their `@[wikidata Q…]` from the branch's Lean
files (handling standalone + stacked forms), drop a now-unused CrossRefAttribute
import, then (with --apply) `lake build`, amend the commit, and force-push so
the open PR becomes the clean green-only set.

  dry-run (default): print the exact edits + the git/build commands it WOULD run.
  --apply:           write files, build, commit --amend, push --force-with-lease.

Usage:
  split.py --mathlib ~/mathlib4 --branch wikilean/wikidata-batch-2 \
           --recycle Q942423,Q120812,Q1783179,Q187235,Q1154787,Q652446 [--apply]
"""
import argparse, subprocess, re, sys
from pathlib import Path

IMPORT_LINE = "public import Mathlib.Tactic.CrossRefAttribute"


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr[:400]}")
    return r


def strip_tag(line, qid):
    """Return the line with `wikidata <qid>` removed, or None to delete the line.

    Handles: standalone `@[wikidata Q]`; stacked `@[a, wikidata Q]` /
    `@[wikidata Q, a]`; and `(attr := … wikidata Q …)` (to_additive)."""
    q = re.escape(qid)
    # standalone attribute line -> delete
    if re.fullmatch(rf"\s*@\[\s*wikidata\s+{q}\s*\]\s*", line):
        return None
    new = line
    # `, wikidata Q`  (tag not first in a list)
    new = re.sub(rf",\s*wikidata\s+{q}\b", "", new)
    # `wikidata Q, `  (tag first in a list)
    new = re.sub(rf"\bwikidata\s+{q}\s*,\s*", "", new)
    # `(attr := wikidata Q)` -> drop the whole attr clause (to_additive)
    new = re.sub(rf"\s*\(attr\s*:=\s*wikidata\s+{q}\s*\)", "", new)
    if new == line:
        # bare `wikidata Q` with no neighbours left -> e.g. `@[wikidata Q]` caught
        # above; otherwise remove the token itself defensively.
        new = re.sub(rf"\bwikidata\s+{q}\b", "", new)
    # if the attribute list is now empty, delete the line
    if re.fullmatch(r"\s*@\[\s*\]\s*", new):
        return None
    return new


def plan(mathlib: Path, recycle):
    """Return [(relpath, [(op, lineno, old, new)])] edits; op = 'del'|'edit'|'import'."""
    edits = {}
    # locate each qid in the checkout
    for qid in recycle:
        hit = run(["grep", "-rln", f"wikidata {qid}", str(mathlib / "Mathlib")], check=False).stdout.split()
        if not hit:
            print(f"  !! {qid}: not found in checkout (already removed?)", file=sys.stderr)
            continue
        path = Path(hit[0])
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, ln in enumerate(lines):
            if re.search(rf"wikidata\s+{re.escape(qid)}\b", ln):
                new = strip_tag(ln, qid)
                edits.setdefault(path, []).append(("del" if new is None else "edit", i, ln, new))
                break
    # drop a now-unused import where no wikidata tag will remain in the file
    for path, ops in list(edits.items()):
        lines = path.read_text(encoding="utf-8").split("\n")
        deleted = {i for op, i, _, _ in ops if op == "del"}
        edited = {i: new for op, i, _, new in ops if op == "edit"}
        remaining = []
        for i, ln in enumerate(lines):
            if i in deleted:
                continue
            cur = edited.get(i, ln)
            if re.search(r"\bwikidata\s+Q\d+", cur):
                remaining.append(cur)
        if not remaining:
            for i, ln in enumerate(lines):
                if ln.strip() == IMPORT_LINE:
                    ops.append(("import", i, ln, None))
    return edits


def apply_edits(edits):
    for path, ops in edits.items():
        lines = path.read_text(encoding="utf-8").split("\n")
        drop = {i for op, i, _, _ in ops if op in ("del", "import")}
        repl = {i: new for op, i, _, new in ops if op == "edit"}
        out = [repl.get(i, ln) for i, ln in enumerate(lines) if i not in drop]
        path.write_text("\n".join(out), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--recycle", required=True, help="comma-separated qids to remove")
    ap.add_argument("--apply", action="store_true", help="write + build + amend + force-push")
    args = ap.parse_args()
    recycle = [q.strip() for q in args.recycle.split(",") if q.strip()]

    print(f"# split: keep greens, remove {len(recycle)} recycled tags from {args.branch}")
    edits = plan(args.mathlib, recycle)
    rel = lambda p: str(p).replace(str(args.mathlib) + "/", "")
    for path, ops in edits.items():
        print(f"\n{rel(path)}")
        for op, i, old, new in ops:
            if op == "import":
                print(f"  L{i+1} drop unused import: {old.strip()}")
            elif new is None:
                print(f"  L{i+1} delete: {old.strip()}")
            else:
                print(f"  L{i+1} edit:   {old.strip()}  ->  {new.strip()}")
    if not args.apply:
        print("\n[dry-run] no files changed. Would then run, in", args.mathlib, ":")
        print("  lake build  (verify)")
        print(f"  git add -A && git commit --amend --no-edit")
        print(f"  git push --force-with-lease origin {args.branch}")
        return

    apply_edits(edits)
    print("\n[apply] files written. building…")
    b = run(["lake", "build"], cwd=args.mathlib, check=False)
    if b.returncode != 0:
        print("BUILD FAILED — not pushing.\n" + b.stdout[-1500:]); sys.exit(1)
    run(["git", "add", "-A"], cwd=args.mathlib)
    run(["git", "commit", "--amend", "--no-edit"], cwd=args.mathlib)
    run(["git", "push", "--force-with-lease", "origin", args.branch], cwd=args.mathlib)
    print("pushed — PR now reflects the green-only set.")


if __name__ == "__main__":
    main()
