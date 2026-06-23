#!/usr/bin/env python3
"""Open the NEXT batch PR — but only once the current PR is MERGED.

Gated on merge so it's safe to run on a timer: it no-ops until the current PR
(state.current_pr) lands, then assembles batch N+1 = requeued retargets (from
triage) + fresh pool tags, opens the PR via open_batch_pr.py, posts the crossref
comments + LLM-generated label + the deterministic reviewer table, advances the
state, and refreshes /queue.

Determinism: tag application is deterministic (open_batch_pr); the only LLM input
is the per-tag retarget the triage already chose (recycle_queue.json).

  open_batch.py --mathlib ~/mathlib4            # dry-run (shows merge state + the assembled batch)
  open_batch.py --mathlib ~/mathlib4 --apply    # open it (only if current PR merged)
"""
import argparse, json, subprocess, sys
from pathlib import Path
import pool, settle

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "bot_state.json"
QUEUE = HERE / "state" / "recycle_queue.json"
CUTLOG = HERE / "state" / "cut_log.json"
REPO = "leanprover-community/mathlib4"
TITLE = "doc: add wikidata attributes"


def sh(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, **kw)


def merged(pr):
    return settle.is_merged(pr, REPO)  # bors-aware (CLOSED + "[Merged by Bors]")


def assemble(batch_num):
    """approved JSON for the next batch: requeued retargets + fresh, to 25."""
    requeued = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    cut = {e["qid"] for e in (json.loads(CUTLOG.read_text()) if CUTLOG.exists() else [])}
    # A requeued tag may correct the declaration, the QID (too-broad concept), or
    # both. Use the triage's suggested_qid/suggested_decl, falling back to the
    # originals; skip any entry with neither a usable decl nor a real change.
    tags = []
    for e in requeued:
        tr = e.get("triage", {})
        if not (tr.get("suggested_decl") or tr.get("suggested_qid")):
            continue
        decl = tr.get("suggested_decl") or e.get("decl")
        if not decl:                       # can't tag without a target declaration
            continue
        tags.append({"qid": tr.get("suggested_qid") or e["qid"], "file": e["file"], "decl": decl})
    # Exclude both the original and corrected QIDs so the pool fill never re-proposes them.
    exclude = {t["qid"] for t in tags} | {e["qid"] for e in requeued} | cut
    fresh = pool.candidates(25 - len(tags), exclude=exclude)
    tags += [{"qid": c["qid"], "file": c["file"], "decl": c["decl"]} for c in fresh]
    branch = f"wikilean/wikidata-batch-{batch_num}"
    return {"batch": batch_num, "title": TITLE, "branch": branch,
            "source": "requeued retargets + most_used pool", "tags": tags}, len(requeued), len(fresh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry = not args.apply
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    cur, batch_num = st.get("current_pr"), st.get("batch_num", 2)
    nxt = batch_num + 1

    if cur and not merged(cur):
        print(f"current PR #{cur} is not merged yet — waiting. (nothing to open)")
        return
    print(f"PR #{cur} is MERGED ✓ — assembling batch {nxt}")

    approved, nq, nf = assemble(nxt)
    print(f"batch {nxt}: {nq} requeued retargets + {nf} fresh = {len(approved['tags'])}")
    for t in approved["tags"]:
        print(f"  {t['qid']:11} -> {t['decl']}")
    apath = HERE / "state" / f"batch{nxt}_approved.json"

    if dry:
        print(f"\n[dry-run] would write {apath} and run:")
        print(f"  (refresh tagged) ; reset checkout to upstream/master")
        print(f"  open_batch_pr.py --approved {apath.name} --mathlib {args.mathlib} --repo {REPO} --base master --apply --check --build --open-pr")
        print(f"  pr_table.py N --post --fresh ; advance state -> batch {nxt}")
        print(f"  finalize.py --pr N (LLM-label + crossref comments + /queue publish)")
        return

    # Refresh the merged-tag set FIRST (the previous batch just landed on master),
    # then RE-assemble so the pool can't re-propose a just-merged tag.
    sh(["python3", str(HERE / "refresh_tagged.py"), "--mathlib", str(args.mathlib)])
    approved, nq, nf = assemble(nxt)
    print(f"after refresh: {nq} requeued + {nf} fresh = {len(approved['tags'])}")
    apath.write_text(json.dumps(approved, indent=1, ensure_ascii=False))
    # Branch the new batch off fresh upstream master.
    sh(["git", "-C", str(args.mathlib), "fetch", "https://github.com/leanprover-community/mathlib4", "master"])
    sh(["git", "-C", str(args.mathlib), "checkout", "-B", approved["branch"], "FETCH_HEAD"])
    cg = sh(["lake", "exe", "cache", "get"], cwd=str(args.mathlib))  # prebuilt oleans for the new master
    if cg.returncode != 0:
        # A failed cache-get on the stateless runner leaves `lake build` to cold-compile
        # Mathlib's whole transitive closure (multi-hour, blows the CI cap). Bail so the
        # next tick retries against a warm cache instead of burning the budget.
        print("lake exe cache get FAILED — not cold-building all of Mathlib; retry next tick")
        sys.exit(1)
    r = sh(["python3", str(HERE / "open_batch_pr.py"), "--approved", str(apath),
            "--mathlib", str(args.mathlib), "--repo", REPO, "--base", "master",
            "--apply", "--check", "--build", "--open-pr"])
    if r.returncode != 0:
        print("open_batch_pr failed — stopping before finalize."); sys.exit(1)

    # New PR number for this branch.
    prn = subprocess.run(["gh", "pr", "list", "--repo", REPO, "--head", approved["branch"],
                          "--json", "number", "--jq", ".[0].number"],
                         capture_output=True, text=True).stdout.strip()
    if not prn:
        print("could not find the new PR (did gh pr create fail?) — stopping before finalize."); sys.exit(1)
    print(f"\nopened PR #{prn}. finalizing…")
    # Deterministic reviewer table (per-tag reviews; idempotent).
    sh([sys.executable, str(HERE / "pr_table.py"), prn, "--repo", REPO, "--post", "--fresh"])
    # Advance state FIRST so a re-run no-ops even if finalize stumbles.
    st.update({"current_pr": int(prn), "batch_num": nxt, "branch": approved["branch"]})
    STATE.write_text(json.dumps(st, indent=1))
    # Finalize — fully automated now (best-effort, finalize.py always exits 0):
    # the REQUIRED LLM-generated label + crossref inline comments + /queue publish.
    sh([sys.executable, str(HERE / "finalize.py"), "--pr", prn,
        "--mathlib", str(args.mathlib), "--approved", str(apath)])


if __name__ == "__main__":
    main()
