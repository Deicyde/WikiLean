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
import pool

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
    out = subprocess.run(["gh", "pr", "view", str(pr), "--repo", REPO, "--json", "state,mergedAt"],
                         capture_output=True, text=True).stdout
    j = json.loads(out or "{}")
    return j.get("state") == "MERGED"


def assemble(batch_num):
    """approved JSON for the next batch: requeued retargets + fresh, to 25."""
    requeued = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    cut = {e["qid"] for e in (json.loads(CUTLOG.read_text()) if CUTLOG.exists() else [])}
    tags = [{"qid": e["qid"], "file": e["file"], "decl": e["triage"]["suggested_decl"]}
            for e in requeued if e.get("triage", {}).get("suggested_decl")]
    exclude = {t["qid"] for t in tags} | cut
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
        print(f"  warm-cache.sh N ; post-wikidata-comments.sh N ; open_batch_pr.py --llm-label --pr N")
        print(f"  pr_table.py N | gh pr comment N ; advance state -> batch {nxt} ; publish /queue")
        return

    apath.write_text(json.dumps(approved, indent=1, ensure_ascii=False))
    # Branch the new batch off fresh upstream master.
    sh(["python3", str(HERE / "refresh_tagged.py"), "--mathlib", str(args.mathlib)])
    sh(["git", "-C", str(args.mathlib), "fetch", "https://github.com/leanprover-community/mathlib4", "master"])
    sh(["git", "-C", str(args.mathlib), "checkout", "-B", approved["branch"], "FETCH_HEAD"])
    r = sh(["python3", str(HERE / "open_batch_pr.py"), "--approved", str(apath),
            "--mathlib", str(args.mathlib), "--repo", REPO, "--base", "master",
            "--apply", "--check", "--build", "--open-pr"])
    if r.returncode != 0:
        print("open_batch_pr failed — stopping before finalize."); sys.exit(1)

    # New PR number for this branch.
    prn = subprocess.run(["gh", "pr", "list", "--repo", REPO, "--head", approved["branch"],
                          "--json", "number", "--jq", ".[0].number"],
                         capture_output=True, text=True).stdout.strip()
    print(f"\nopened PR #{prn}. finalizing…")
    # Deterministic reviewer table (per-tag reviews; idempotent).
    sh([sys.executable, str(HERE / "pr_table.py"), prn, "--repo", REPO, "--post", "--fresh"])
    # Advance state so a re-run no-ops (idempotent open).
    st.update({"current_pr": int(prn), "batch_num": nxt, "branch": approved["branch"]})
    STATE.write_text(json.dumps(st, indent=1))
    # NOTE (supervised on first batch-3): crossref comments + LLM-generated label
    # still run manually — both need the brew-bash + CROSSREF_DIFF_FILE workaround:
    print(f"  then run (supervised): warm-cache.sh {prn} ; post-wikidata-comments.sh {prn} ; "
          f"open_batch_pr.py --llm-label --pr {prn} ; publish_queue.py --candidates <fresh>")


if __name__ == "__main__":
    main()
