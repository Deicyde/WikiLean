#!/usr/bin/env python3
"""@[wikidata] batch bot — manual all-in-one orchestrator. DRY-RUN by default.

NOTE: the production trigger is event-driven `poll.py` (acts on GitHub state —
merge → open, gate-met → settle), NOT this timer-style one-shot. Keep daily_bot
for manual/testing runs; use poll.py (--watch) for the live loop.

Two phases per run:
  SETTLE the current PR (deterministic): if the gate is open, split it down to
    its green tags (force-push), then LLM-triage the recycled tags into the queue
    and post a ready-to-merge comment.
  OPEN the next batch: requeued (retargeted) tags + fresh pool tags -> 25 ->
    open_batch_pr.py.

Determinism boundary (Jack's rule): everything here is deterministic EXCEPT the
recycle triage (triage.py), which is the one sanctioned LLM step. Tag generation
stays deterministic — the LLM only proposes a retarget declaration; open_batch_pr
applies it.

Safety: nothing mutates GitHub/Mathlib unless --apply is given. Run --apply by
hand for the first cycles; only then wrap in cron.
"""
import argparse, json, subprocess, sys
from pathlib import Path
import settle, pool

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "bot_state.json"          # {current_pr, batch_num, branch}
QUEUE = HERE / "state" / "recycle_queue.json"      # requeued tags (retargets) for next batch
REPO = "leanprover-community/mathlib4"


def sh(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True, help="local mathlib4 checkout (PR branch)")
    ap.add_argument("--apply", action="store_true", help="actually split/triage/open (else dry-run)")
    ap.add_argument("--model", default="", help="model for triage.py")
    args = ap.parse_args()
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    pr = st.get("current_pr")
    branch = st.get("branch")
    dry = not args.apply
    tag = "[dry-run] " if dry else "[apply] "

    # ---- SETTLE the current PR ----
    if pr:
        r = settle.classify(pr, REPO)
        green = [g["qid"] for g in r["green"]]
        recycle = r["recycle"]
        # Enrich recycled entries with the tagged decl (triage needs it for
        # QID-only corrections); the opened batch's approved JSON carries it.
        try:
            appr = json.loads((HERE / "state" / f"batch{st.get('batch_num')}_approved.json").read_text())
            decl_by_qid = {t["qid"]: t["decl"] for t in appr.get("tags", [])}
            for e in recycle:
                e.setdefault("decl", decl_by_qid.get(e["qid"]))
        except Exception:
            pass
        print(f"PR #{pr} · age {r['age_h']}h · reviewers {r['reviewers']} · "
              f"gate {'OPEN' if r['gate'] else 'WAIT'}")
        print(f"  green {len(green)} · recycle {len(recycle)}")
        if not r["gate"]:
            print("  gate not open — nothing to settle today.")
        else:
            # 1) deterministic split to greens
            recycle_qids = ",".join(e["qid"] for e in recycle)
            print(f"\n{tag}SPLIT to {len(green)} greens (drop {len(recycle)}):")
            cmd = [sys.executable, str(HERE / "split.py"), "--mathlib", str(args.mathlib),
                   "--branch", branch, "--recycle", recycle_qids] + (["--apply"] if not dry else [])
            sh(cmd)
            # 2) LLM triage of recycled tags -> queue
            print(f"\n{tag}TRIAGE {len(recycle)} recycled tags (LLM):")
            if dry:
                print("  (dry-run) would call triage.py --in <recycle> (LLM requeue/cut)")
            else:
                p = subprocess.run([sys.executable, str(HERE / "triage.py"),
                                    "--out-queue", str(QUEUE), "--model", args.model],
                                   input=json.dumps(recycle), text=True)
            # 3) tag table = trim/ready notice (one idempotent comment: green table
            #    + ready-to-merge + recycled summary; template: trim comment on #40682)
            header = (f"This PR was trimmed to the **{len(green)}** `@[wikidata]` tags approved 🟢 "
                      f"by ≥2 reviewers (incl. a maintainer) — **ready to merge**."
                      + (f" {len(recycle)} recycled to the next batch." if recycle else "")
                      + " <!-- wikilean-bot-ready -->")
            print(f"\n{tag}TABLE+READY on #{pr}: pr_table.py --post --header …")
            if dry:
                print("  " + header)
            else:
                sh([sys.executable, str(HERE / "pr_table.py"), str(pr), "--repo", REPO,
                    "--post", "--header", header])
    else:
        print("no current PR in state — first run will just open a batch.")

    # ---- BUILD next batch + REFRESH the public queue ----
    requeued = json.loads(QUEUE.read_text()) if QUEUE.exists() else []  # triage output
    inflight = set(r["tags"]) if pr else set()
    inflight |= {e["qid"] for e in requeued}
    need = max(0, 25 - len(requeued))
    if not dry:  # keep the live tagged-set current before selecting (skips in dry-run)
        sh([sys.executable, str(HERE / "refresh_tagged.py"), "--mathlib", str(args.mathlib)])
    fresh = pool.candidates(need, exclude=inflight)  # deterministic pool selector (+ P31 field filter)
    print(f"\n{tag}NEXT BATCH: {len(requeued)} requeued (retargeted) + {len(fresh)} fresh = "
          f"{len(requeued) + len(fresh)}/25")
    for e in requeued:
        t = e.get("triage", {})
        print(f"  requeue {e['qid']} -> {t.get('suggested_decl','?')}")
    for c in fresh[:6]:
        print(f"  fresh   {c['qid']} {c['label'][:30]:30} -> {c['decl']}")
    if len(fresh) > 6:
        print(f"  … +{len(fresh)-6} more fresh")

    # Publish recycled + unreviewed to the wiki /queue page.
    cand_file = HERE / "state" / "pool_candidates.json"
    print(f"\n{tag}PUBLISH queue (/queue): {len(requeued)} recycled + {len(fresh)} unreviewed")
    if dry:
        print("  (dry-run) would POST recycled + unreviewed to /api/queue")
    else:
        cand_file.write_text(json.dumps(fresh))
        cmd = [sys.executable, str(HERE / "publish_queue.py"), "--candidates", str(cand_file)]
        if QUEUE.exists():
            cmd += ["--recycle", str(QUEUE)]
        sh(cmd)

    print(f"\n{tag}OPEN PR: open_batch_pr.py --apply --check --build --open-pr  "
          f"(requeued retargets + fresh) + crossref comments + LLM-label + table")
    if dry:
        print("\n[dry-run] complete — nothing mutated. Re-run with --apply to act.")


if __name__ == "__main__":
    main()
