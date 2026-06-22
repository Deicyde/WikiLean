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


def freshen_master(mathlib: Path) -> bool:
    """Merge current upstream/master into HEAD. A STALE merged-master is what
    breaks CI's 'verify that everything was available in the cache' (Post-Build
    Step): when a deeply-imported file (e.g. Module.Defs) is tagged on top of an
    old master, its olean — and its downstream subtree — fall outside the warm
    cache and lake reports 'target is out-of-date'. Merging current master
    realigns them (this is the fix a human used to unblock #40747). Best-effort:
    aborts and returns False on the (rare, for doc-only attrs) merge conflict."""
    run(["git", "fetch", "upstream", "master"], cwd=mathlib, check=False)
    m = run(["git", "merge", "--no-edit", "upstream/master"], cwd=mathlib, check=False)
    if m.returncode != 0:
        run(["git", "merge", "--abort"], cwd=mathlib, check=False)
        return False
    return True


def freshen_branch_and_push(branch: str, mathlib: Path) -> bool:
    """Reactive twin of freshen_master for an ALREADY-settled PR (poller
    self-heal): fetch the branch, merge current master, push. Returns True iff a
    new merge commit was pushed (False if already up to date, or a conflict
    blocked it — caller can then fall back to a plain CI re-trigger)."""
    run(["git", "fetch", "origin", branch], cwd=mathlib)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=mathlib)
    before = run(["git", "rev-parse", "HEAD"], cwd=mathlib).stdout.strip()
    if not freshen_master(mathlib):
        return False
    after = run(["git", "rev-parse", "HEAD"], cwd=mathlib).stdout.strip()
    if before == after:
        return False  # already current — nothing to push
    run(["git", "push", "origin", branch], cwd=mathlib)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--recycle", required=True, help="comma-separated qids to remove")
    ap.add_argument("--apply", action="store_true", help="write + build + amend + force-push")
    ap.add_argument("--no-build", action="store_true", help="skip the local build (CI verifies)")
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
        print("\n[dry-run] no files changed. On --apply, would (on the CURRENT remote tip):")
        print(f"  git fetch origin {args.branch} && git reset --hard origin/{args.branch}")
        print("  git merge upstream/master   (freshen so the cache-verify passes)")
        print("  <re-apply removals>  +  lake build <touched modules>")
        print('  git commit -m "doc: drop recycled @[wikidata] tags pending re-review"')
        print(f"  git push origin {args.branch}   (fast-forward, no --force)")
        return

    # Operate on the CURRENT remote tip: the branch commonly has master merged in
    # between open and settle, so we add a NEW commit on top (plain push) rather
    # than amend+force-push (which would clobber that merge — and a stale lease
    # would reject it anyway).
    run(["git", "fetch", "origin", args.branch], cwd=args.mathlib)
    # checkout -B (NOT reset --hard): on a fresh Actions clone the working dir is on
    # `master` with NO local branch by this name, so a later `git push origin <branch>`
    # dies with "src refspec … does not match any". -B creates/points the local branch
    # at the fetched tip, giving the push a source ref. (On the laptop the branch was
    # already checked out from the open, so reset --hard happened to work.)
    run(["git", "checkout", "-B", args.branch, "FETCH_HEAD"], cwd=args.mathlib)
    # Freshen against current master BEFORE trimming so the build-cache-verify
    # passes (a stale merged-master leaves core-file oleans uncached).
    print("  freshened against upstream/master" if freshen_master(args.mathlib)
          else "  (upstream/master merge conflicted — trimming on the branch tip)")
    edits = plan(args.mathlib, recycle)  # re-plan against the fresh tip
    if not edits:
        print("nothing to remove on the current tip — done."); return
    apply_edits(edits)
    # Build ONLY the touched modules — removing a doc attribute + unused import is
    # semantically inert, so this is a fast confidence check (CI re-verifies).
    mods = ["Mathlib." + str(p).split("/Mathlib/", 1)[1].replace(".lean", "").replace("/", ".")
            for p in edits]
    print("\n[apply] files written. building touched modules:", mods)
    if not args.no_build:
        b = run(["lake", "build", *mods], cwd=args.mathlib, check=False)
        if b.returncode != 0:
            print("BUILD FAILED — restoring files, not pushing.\n" + b.stdout[-1500:])
            run(["git", "checkout", "--", *[str(p) for p in edits]], cwd=args.mathlib, check=False)
            sys.exit(1)
        print("  build OK")
    # Stage ONLY the files we edited — NEVER `git add -A`: this checkout may be shared
    # with other projects, and -A would sweep their in-progress files into our commit
    # (exactly how a foreign file leaked into #40747).
    run(["git", "add", "--", *[str(p) for p in edits]], cwd=args.mathlib)
    # Leak guard: the staged trim must be modifications of our files only — no new,
    # deleted, or renamed files may have crept in.
    ns = run(["git", "diff", "--cached", "--name-status"], cwd=args.mathlib).stdout.strip().splitlines()
    nonmod = [l for l in ns if l and not l.startswith("M\t")]
    if nonmod:
        sys.exit("LEAK GUARD: trim staged new/deleted/renamed file(s) — refusing to push:\n  " + "\n  ".join(nonmod))
    run(["git", "commit", "-m", "doc: drop recycled @[wikidata] tags pending re-review"], cwd=args.mathlib)
    run(["git", "push", "origin", args.branch], cwd=args.mathlib)  # fast-forward, no force
    print("pushed — PR now reflects the green-only set.")


if __name__ == "__main__":
    main()
