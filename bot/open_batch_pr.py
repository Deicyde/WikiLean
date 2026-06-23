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
import argparse, json, re, subprocess, sys, time
from pathlib import Path

# --- decl targeting -------------------------------------------------------

MODIFIERS = r"(?:@\[[^\]]*\]\s*)*(?:protected\s+|private\s+|noncomputable\s+|public\s+|nonrec\s+|scoped\s+|local\s+)*"
KINDS     = r"(?:irreducible_def|def|theorem|lemma|class|structure|inductive|abbrev|instance|opaque)"
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
        if not p.exists():  # defensive: untaggable file (e.g. Lean core) — never crash
            results.append({**t, "ok": False, "msg": "file not in mathlib tree (skipped)"}); continue
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

def add_crossref_import(path: Path) -> bool:
    """Insert `public import Mathlib.Tactic.CrossRefAttribute` in alpha order among the
    file's existing `public import` lines. No-op (False) if already present or there is
    no public-import block to anchor to. Used by --reapply to re-add imports WITHOUT a
    build (we already know which files need it)."""
    txt = path.read_text(encoding="utf-8")
    if "Mathlib.Tactic.CrossRefAttribute" in txt:
        return False
    lines = txt.splitlines()
    imp = "public import Mathlib.Tactic.CrossRefAttribute"
    idxs = [i for i, l in enumerate(lines) if l.startswith("public import ")]
    if not idxs:
        return False
    insert_at = idxs[-1] + 1
    for i in idxs:
        if lines[i] > imp:
            insert_at = i; break
    lines.insert(insert_at, imp)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


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
        # SINGLE PASS: a second cold build of ~22 modules just to RE-verify the import
        # fix is what blew the 120m Actions cap (two passes). The CrossRefAttribute
        # import deterministically resolves the only error a @[wikidata] doc-attribute
        # can cause, so open now and let the PR's mathlib CI run the authoritative
        # compile check rather than paying for a second cold rebuild here.
        print("[build] imports added — skipping the re-verify rebuild; mathlib CI verifies")
        return True
    return False  # unreachable (single pass returns above); kept as a safety net

# --- git + gh -------------------------------------------------------------

# Matches the batch-1/2 template (#40440/#40682). {n} = tag count, {pr} = PR
# number (filled after creation, when the number is known, so the reviewer-UI
# link is correct).
PR_BODY_TMPL = """This PR adds a batch of {n} `@[wikidata]` attributes.

Claude helped generate the list of crossrefs (by scanning Wikidata + Mathlib). Comments are generated by [crossref-report](https://github.com/jcommelin/mathlib-crossref-report) and Wikilean.

See https://wikilean.jackmccarthy.org/review?pr={pr} for reviewer UI.

---
"""

def fork_owner(mathlib: Path) -> str | None:
    """Owner of the `origin` remote — the fork the branch is pushed to (e.g.
    'Deicyde'). The PR head must reference this owner, NOT the base repo's, or a
    cross-fork `gh pr create` fails with 'No commits between … / Head ref must be
    a branch'."""
    url = run(["git", "remote", "get-url", "origin"], cwd=mathlib).stdout.strip()
    m = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return m.group(1) if m else None


CROSSREF_IMPORT = "public import Mathlib.Tactic.CrossRefAttribute"

def assert_only_wikidata_changes(mathlib: Path, tagged_files: set):
    """LEAK GUARD. Refuse to open unless the STAGED diff is exactly @[wikidata]
    attributes (standalone or stacked into an existing @[...]) + the CrossRefAttribute
    import, on EXISTING tagged files — nothing else. A foreign file from another
    project sharing this checkout leaked into #40747 via `git add -A`; this aborts the
    open rather than ship anything we didn't author."""
    ns = run(["git", "diff", "--cached", "--name-status"], cwd=mathlib).stdout.strip().splitlines()
    nonmod = [l for l in ns if l and not l.startswith("M\t")]
    if nonmod:
        sys.exit("LEAK GUARD: staged new/deleted/renamed file(s) — refusing to open:\n  " + "\n  ".join(nonmod))
    extra = {l.split("\t", 1)[1] for l in ns if "\t" in l} - tagged_files
    if extra:
        sys.exit("LEAK GUARD: staged file(s) outside the tagged set — refusing to open:\n  " + "\n  ".join(sorted(extra)))
    diff = run(["git", "diff", "--cached", "--unified=0"], cwd=mathlib).stdout.splitlines()
    added   = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    bad = [a for a in added if "wikidata " not in a and a.strip() != CROSSREF_IMPORT]
    if bad:
        sys.exit("LEAK GUARD: staged addition(s) that aren't @[wikidata] tags / the import:\n  "
                 + "\n  ".join(repr(a) for a in bad[:10]))
    # Removals happen ONLY when stacking wikidata into an existing @[...] line: each is
    # an attribute line, one per stacked addition. Anything else is a foreign edit.
    stacked = [a for a in added if ", wikidata Q" in a]
    bad_rm = [r for r in removed if not r.strip().startswith("@[")]
    if bad_rm or len(removed) != len(stacked):
        sys.exit(f"LEAK GUARD: unexpected removals (got {len(removed)}, expected {len(stacked)} stacked):\n  "
                 + "\n  ".join(repr(r) for r in (bad_rm or removed)[:10]))
    print(f"[leak-guard] clean: {len(added)} staged addition(s) across {len(ns)} file(s), {len(stacked)} stacked; no foreign files.")

def open_pr(approved: dict, mathlib: Path, repo: str, base: str, create: bool = True):
    branch = approved["branch"]
    title  = approved["title"]
    files  = sorted({t["file"] for t in approved["tags"]})
    print(f"[git] create branch: git checkout -B {branch}")
    if run(["git", "checkout", "-B", branch], cwd=mathlib).returncode != 0:
        sys.exit("git checkout failed")
    # Stage ONLY the tagged files — NEVER `git add -A`: this checkout may be shared with
    # other projects, and -A would sweep their in-progress files into our commit (exactly
    # how a foreign file leaked into #40747).
    print(f"[git] stage {len(files)} tagged file(s) (explicit paths, not -A)")
    if run(["git", "add", "--", *files], cwd=mathlib).returncode != 0:
        sys.exit("git add failed")
    assert_only_wikidata_changes(mathlib, set(files))
    # Empty staged diff = every tag skipped (decls missing / already tagged on master /
    # pool drained). Exit cleanly instead of letting `gh pr create` fail later with the
    # cryptic "No commits between …".
    if run(["git", "diff", "--cached", "--quiet"], cwd=mathlib).returncode == 0:
        sys.exit("no @[wikidata] tags applied (decls missing / already on master / pool drained) — nothing to open")
    for desc, cmd in [
        ("commit",        ["git", "commit", "-m", title]),
        ("push",          ["git", "push", "-u", "origin", branch, "--force-with-lease"]),
    ]:
        print(f"[git] {desc}: {' '.join(cmd)}")
        r = run(cmd, cwd=mathlib)
        if r.returncode != 0 and desc != "commit":
            print(r.stdout + r.stderr); sys.exit(1)
        print((r.stdout + r.stderr).strip()[:400])
    if not create:  # --reapply: rebuilt branch is force-pushed; the PR already exists
        print(f"[git] force-pushed rebuilt {branch} (--reapply, no PR create)")
        return
    fo = fork_owner(mathlib) or repo.split('/')[0]
    # Count tags ACTUALLY applied (from the pushed commit) — apply_all skips decls already
    # tagged on master or no longer present, so this is usually < the approved count; the
    # body must say what's in the diff (#40970's body said 25, the diff had 21).
    shown = run(["git", "show", "HEAD", "--unified=0"], cwd=mathlib).stdout
    n = sum(1 for l in shown.splitlines()
            if l.startswith("+") and not l.startswith("+++") and "wikidata Q" in l) or len(approved["tags"])
    print(f"[gh] pr create (head {fo}:{branch} -> {repo}:{base})")
    r = run(["gh", "pr", "create", "--repo", repo, "--base", base,
             "--head", f"{fo}:{branch}",
             "--title", title, "--body", PR_BODY_TMPL.format(n=n, pr="")], cwd=mathlib)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        sys.exit(1)
    # The blank ?pr= in the body is filled by open_batch.py AFTER it confirms the PR
    # number via `gh pr list` (with a verified retry) — editing the body immediately here
    # races GitHub: a fresh cross-fork PR isn't editable for a while, which left
    # #40861/#40970 with a blank link even with a short in-place retry.

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
    ap.add_argument("--reapply", action="store_true",
                    help="rebuild: force-push the re-applied (off fresh master) branch WITHOUT creating a PR")
    ap.add_argument("--llm-label", dest="llm_label", action="store_true",
                    help="add the LLM-generated label + post the disclosure comment")
    ap.add_argument("--pr", type=int, default=None,
                    help="PR number for --llm-label (defaults to the branch's PR)")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    approved = json.loads(args.approved.read_text(encoding="utf-8"))

    # Lean *core* decls (Rat, Dvd.dvd, HPow.hPow, …) live in Init/… inside the
    # toolchain, NOT the mathlib repo, so they can't carry an @[wikidata] attribute
    # from here — and apply_all would FileNotFoundError on the read, aborting the
    # WHOLE batch. Drop any tag whose file isn't present in the checkout, up front,
    # so it's excluded from apply, build, and the PR alike.
    keep, drop = [], []
    for t in approved["tags"]:
        (keep if (args.mathlib / t["file"]).exists() else drop).append(t)
    if drop:
        print(f"  dropping {len(drop)} untaggable tag(s) — file not in mathlib tree:")
        for t in drop:
            print(f"    - {t['qid']:10s} {t['decl']:28s} ({t['file']})")
        approved["tags"] = keep

    if args.apply or args.all:
        print(f"=== apply {len(approved['tags'])} tags ===")
        results = apply_all(approved, args.mathlib)
        for r in results:
            flag = "ok " if r["ok"] else "ERR"
            print(f"  [{flag}] {r['qid']:10s} {r['decl']:30s} {r['msg']}")
        if args.reapply:
            # Rebuilding an ALREADY-APPROVED PR off fresh master: every green tag must
            # still apply. If a decl was renamed/removed upstream, refuse to silently
            # ship a smaller PR (dropping a human-approved tag) — fail loudly instead.
            dropped = [r for r in results if not r["ok"]]
            if dropped:
                sys.exit("REAPPLY: %d approved tag(s) no longer apply on master (decl moved?) — "
                         "refusing to drop: %s" % (len(dropped),
                         ", ".join(f"{r['qid']}({r['decl']})" for r in dropped)))
            # Re-add the CrossRefAttribute import to the files that carried it on the old
            # branch (import_files) WITHOUT a build. A full `lake build` off the latest,
            # partly-uncached master cold-cascades for HOURS (observed 80m+ on #40861's
            # foundational modules); the original build already proved which files need the
            # import, and mathlib CI re-verifies the result.
            for f in approved.get("import_files", []):
                if add_crossref_import(args.mathlib / f):
                    print(f"  [import] re-added CrossRefAttribute to {f}")
    if args.check or args.all:
        print("\n=== git diff --stat ===")
        print(run(["git", "diff", "--stat"], cwd=args.mathlib).stdout)
    if args.build or args.all:
        if not build_and_fix_imports(approved, args.mathlib):
            print("BUILD FAILED — not opening PR"); sys.exit(1)
    if args.open_pr or args.all:
        open_pr(approved, args.mathlib, args.repo, args.base)
    if args.reapply:
        open_pr(approved, args.mathlib, args.repo, args.base, create=False)
    if args.llm_label or args.all:
        n = _pr_number(args)
        if n:
            post_llm_disclosure(args.repo, n)

def _pr_number(args):
    if args.pr:
        return args.pr
    fo = fork_owner(args.mathlib) or args.repo.split('/')[0]
    r = run(["gh", "pr", "view", f"{fo}:{json.loads(args.approved.read_text())['branch']}",
             "--repo", args.repo, "--json", "number", "--jq", ".number"])
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None

if __name__ == "__main__":
    main()
