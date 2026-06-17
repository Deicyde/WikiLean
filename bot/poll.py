#!/usr/bin/env python3
"""Event-driven trigger for the @[wikidata] batch bot — polls GitHub, no timer.

Each tick looks at the current batch PR's GitHub state and acts on it:
  - MERGED                         -> open the next batch (open_batch.py)
  - OPEN + settle gate met         -> trim to greens + triage/queue the rest
    (>=2 reviewers incl >=1 maintainer)   + post the reviewer table + ready comment
  - otherwise                      -> wait (nothing to do)

Idempotent via state.settled_pr (won't re-settle) and merge state (won't
re-open). Dry-run by default — prints the decision; --apply acts.

  poll.py --mathlib ~/mathlib4                     # one dry-run tick (decision only)
  poll.py --mathlib ~/mathlib4 --apply             # one tick, act
  poll.py --mathlib ~/mathlib4 --apply --watch 600 # poll every 600s (launchd/cron)
"""
import argparse, json, subprocess, sys, time
from pathlib import Path
import settle, pool

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "bot_state.json"
QUEUE = HERE / "state" / "recycle_queue.json"
CANDS = HERE / "state" / "pool_candidates.json"
REPO = "leanprover-community/mathlib4"


def sh(cmd, **kw):
    print("    $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, **kw)


def gh_state(pr):
    return subprocess.run(["gh", "pr", "view", str(pr), "--repo", REPO, "--json", "state", "--jq", ".state"],
                          capture_output=True, text=True).stdout.strip() or "?"


def do_settle(pr, branch, mathlib, cls, dry):
    recycle = cls["recycle"]
    qids = ",".join(e["qid"] for e in recycle)
    green = len(cls["green"])
    print(f"  SETTLE #{pr}: {green} green / {len(recycle)} recycle "
          f"(reviewers {cls['reviewers']}, maintainers {cls['maintainer_reviewers']})")
    if dry:
        print(f"    would: split --recycle {qids}; triage (LLM); publish /queue; pr_table --post; ready comment")
        return
    sh([sys.executable, str(HERE / "split.py"), "--mathlib", str(mathlib), "--branch", branch,
        "--recycle", qids, "--apply", "--no-build"])
    sh([sys.executable, str(HERE / "triage.py"), "--out-queue", str(QUEUE)], input=json.dumps(recycle))
    sh([sys.executable, str(HERE / "pr_table.py"), str(pr), "--repo", REPO, "--post"])
    fresh = pool.candidates(20, exclude=set(cls["tags"]) | {e["qid"] for e in recycle})
    CANDS.write_text(json.dumps(fresh))
    sh([sys.executable, str(HERE / "publish_queue.py"), "--recycle", str(QUEUE), "--candidates", str(CANDS)])
    body = (f"**WikiLean bot:** {green} tag(s) approved (≥2 reviewers, ≥1 maintainer) — ready to merge. "
            f"{len(recycle)} recycled to the next batch. <!-- wikilean-bot-ready -->")
    sh(["gh", "pr", "comment", str(pr), "--repo", REPO, "--body", body])


def do_open(mathlib, dry):
    print("  OPEN next batch (open_batch.py)")
    sh([sys.executable, str(HERE / "open_batch.py"), "--mathlib", str(mathlib)] + ([] if dry else ["--apply"]))


def tick(mathlib, dry, no_open=False):
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    pr, branch = st.get("current_pr"), st.get("branch")
    if not pr:
        print("no current PR in state — opening first batch")
        if not no_open:
            do_open(mathlib, dry)
        return
    state = gh_state(pr)
    print(f"poll #{pr}: state={state}  settled={st.get('settled_pr') == pr}")
    if state == "MERGED":
        if no_open:
            print("  MERGED ✓ — open the next batch (supervised): "
                  "`poll.py --apply` without --no-open, or open_batch.py --apply"); return
        do_open(mathlib, dry)
        if not dry:  # open_batch advanced current_pr; clear the stale settled marker
            st = json.loads(STATE.read_text()); st.pop("settled_pr", None); STATE.write_text(json.dumps(st, indent=1))
        return
    if state != "OPEN":
        print(f"  #{pr} is {state} (not merged) — needs manual attention; skipping"); return
    if st.get("settled_pr") == pr:
        print("  already settled — waiting for merge"); return
    cls = settle.classify(pr, REPO)
    if not cls["gate"]:
        print(f"  gate not met ({cls['gate_reasons']}) — waiting for reviews"); return
    do_settle(pr, branch, mathlib, cls, dry)
    if not dry:
        st["settled_pr"] = pr; STATE.write_text(json.dumps(st, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--watch", type=int, default=0, help="poll every N seconds (0 = one tick)")
    ap.add_argument("--no-open", action="store_true",
                    help="on merge, alert instead of auto-opening the next batch (supervise the first open)")
    args = ap.parse_args()
    dry = not args.apply
    while True:
        tick(args.mathlib, dry, args.no_open)
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
