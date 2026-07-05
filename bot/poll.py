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
import argparse, json, os, subprocess, sys, time
from pathlib import Path
import settle, pool, split

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "bot_state.json"
QUEUE = HERE / "state" / "recycle_queue.json"
CANDS = HERE / "state" / "pool_candidates.json"
REPO = "leanprover-community/mathlib4"


def sh(cmd, check=False, **kw):
    print("    $", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, text=True, **kw)
    # check=True for steps where failure means the tick didn't accomplish its job
    # (open a batch, trim a settle). Exit non-zero so the WORKFLOW step fails loudly
    # instead of reporting 'success' while nothing advanced. Best-effort steps
    # (triage/harvest/pr_table/publish/finalize) stay unchecked — they may stumble.
    if check and r.returncode != 0:
        name = Path(str(cmd[1])).name if len(cmd) > 1 else str(cmd[0])
        sys.exit(f"  ✗ {name} failed (exit {r.returncode}) — failing the tick")
    return r


def gh_state(pr):
    return subprocess.run(["gh", "pr", "view", str(pr), "--repo", REPO, "--json", "state", "--jq", ".state"],
                          capture_output=True, text=True).stdout.strip() or "?"


def do_settle(pr, branch, mathlib, cls, dry):
    recycle = cls["recycle"]
    # Enrich each recycled entry with the decl that was tagged — triage needs it
    # for QID-only corrections (right decl, too-broad QID), where the decl stays
    # the same. settle's diff parse doesn't carry the decl name; the opened
    # batch's approved JSON does.
    try:
        bn = json.loads(STATE.read_text()).get("batch_num")
        appr = json.loads((HERE / "state" / f"batch{bn}_approved.json").read_text())
        decl_by_qid = {t["qid"]: t["decl"] for t in appr.get("tags", [])}
        for e in recycle:
            e.setdefault("decl", decl_by_qid.get(e["qid"]))
    except Exception as ex:
        print(f"  (could not enrich recycle decls: {ex})")
    qids = ",".join(e["qid"] for e in recycle)
    green = len(cls["green"])
    print(f"  SETTLE #{pr}: {green} green / {len(recycle)} recycle "
          f"(reviewers {cls['reviewers']}, maintainers {cls['maintainer_reviewers']})")
    if dry:
        print(f"    would: split --recycle {qids}; triage (LLM); publish /queue; pr_table --post; ready comment")
        return
    sh([sys.executable, str(HERE / "split.py"), "--mathlib", str(mathlib), "--branch", branch,
        "--recycle", qids, "--apply", "--no-build"], check=True)
    sh([sys.executable, str(HERE / "triage.py"), "--out-queue", str(QUEUE)], input=json.dumps(recycle))
    # Feedback loop (deterministic, idempotent): harvest every reviewer
    # reject/revise (corrections, with the narrower QID they named) + approve
    # "also tag X" notes (additions) into the dataset, then apply the explicit
    # QID fixes to the requeue and collect the additions. Grows the few-shot
    # corpus tag_with_mathlib learns from, and ensures requeued tags carry the
    # CORRECTED QID rather than repeating the rejected broad one.
    sh([sys.executable, str(HERE / "harvest_corrections.py"), str(pr), "--repo", REPO])
    sh([sys.executable, str(HERE / "resolve_concepts.py")])   # fill QIDs the note named but didn't number
    sh([sys.executable, str(HERE / "apply_corrections.py")])
    # The tag table IS the trim/ready notice: ONE idempotent comment carrying the
    # green-only table + the ready-to-merge + recycled summary, rather than a
    # separate bare line (template: the trim comment on #40682).
    header = (f"This PR was trimmed to the **{green}** `@[wikidata]` tags approved 🟢 "
              f"by ≥2 reviewers (incl. a maintainer) — **ready to merge**."
              + (f" {len(recycle)} recycled to the next batch." if recycle else "")
              + " <!-- wikilean-bot-ready -->")
    sh([sys.executable, str(HERE / "pr_table.py"), str(pr), "--repo", REPO, "--post", "--header", header,
        "--no-body-sync"])
    # The trim shrank the diff to the green set; re-sync the PR body's count to match.
    # Use the KNOWN green count, not a `gh pr diff` recount — GitHub can still serve the
    # pre-trim diff for a moment after split's push, and a settled PR never re-syncs.
    import pr_table
    pr_table.sync_body_count(pr, REPO, n=green)
    fresh = pool.candidates(20, exclude=set(cls["tags"]) | {e["qid"] for e in recycle})
    CANDS.write_text(json.dumps(fresh))
    sh([sys.executable, str(HERE / "publish_queue.py"), "--recycle", str(QUEUE), "--candidates", str(CANDS)])


def pr_conflicting(pr):
    return subprocess.run(["gh", "pr", "view", str(pr), "--repo", REPO, "--json", "mergeable", "--jq", ".mergeable"],
                          capture_output=True, text=True).stdout.strip() == "CONFLICTING"


def master_tip():
    return subprocess.run(["gh", "api", f"repos/{REPO}/commits/master", "--jq", ".sha"],
                          capture_output=True, text=True).stdout.strip()


def resolve_conflicts(pr, branch, mathlib, dry):
    """Auto-resolve a settled PR that CONFLICTS with master: RE-DERIVE the branch off
    FRESH master and re-apply its green tags, then force-push. @[wikidata] tags are
    purely ADDITIVE, so rebuilding on master is conflict-proof — text-merging master in
    (freshen_master) is what kept hitting unresolvable conflicts on doc-attr PRs."""
    cls = settle.classify(pr, REPO)
    bn = json.loads(STATE.read_text()).get("batch_num")
    approved = json.loads((HERE / "state" / f"batch{bn}_approved.json").read_text())
    # The green set = tags CURRENTLY on the PR (post-trim the recycled are already gone,
    # so classify(pr).recycle is empty — reconstruct from classify.green, not approved
    # minus recycled, which would wrongly re-add the trimmed tags). decl/file from approved.
    green_qids = {g["qid"] for g in cls["green"]}
    green = {"branch": branch, "title": approved.get("title", "doc: add wikidata attributes"),
             "tags": [t for t in approved["tags"] if t["qid"] in green_qids]}
    print(f"  RESOLVE-CONFLICT #{pr}: rebuild off fresh master with {len(green['tags'])} green tags")
    if dry:
        return
    sh(["git", "-C", str(mathlib), "fetch", "origin", branch], check=True)       # --force-with-lease base
    sh(["git", "-C", str(mathlib), "fetch", "upstream", "master"], check=True)   # FETCH_HEAD = current master
    rebuilt = subprocess.run(["git", "-C", str(mathlib), "rev-parse", "FETCH_HEAD"],
                             capture_output=True, text=True).stdout.strip()
    # Carry over the import set from the EXISTING branch so the rebuild needs NO build:
    # a `lake build` off the latest, partly-uncached master cold-cascades for HOURS on
    # foundational modules (it ran 80m+ on #40861 before timing out). The original build
    # already proved which files need the CrossRefAttribute import; re-add exactly those
    # and let mathlib CI do the compile check.
    green_files = {t["file"] for t in green["tags"]}
    gr = subprocess.run(["git", "-C", str(mathlib), "grep", "-l", "Mathlib.Tactic.CrossRefAttribute",
                         f"origin/{branch}", "--", "Mathlib"], capture_output=True, text=True).stdout.split()
    green["import_files"] = sorted({f.split(":", 1)[1] for f in gr if ":" in f} & green_files)
    # Record the master we rebuild ONTO *before* the fallible push, so a failed rebuild
    # doesn't re-loop every tick at the same master — it retries only when master advances.
    stt = json.loads(STATE.read_text()); stt["conflict_sha"] = rebuilt; stt.pop("retriggered_pr", None)
    STATE.write_text(json.dumps(stt, indent=1))
    gpath = HERE / "state" / "reapply_green.json"
    gpath.write_text(json.dumps(green, indent=1))
    sh(["git", "-C", str(mathlib), "checkout", "-B", branch, "FETCH_HEAD"], check=True)
    sh([sys.executable, str(HERE / "open_batch_pr.py"), "--approved", str(gpath),
        "--mathlib", str(mathlib), "--repo", REPO, "--base", "master",
        "--apply", "--check", "--reapply"], check=True)   # NO --build — CI compile-checks
    # The force-push shrank the diff to the green set; this path otherwise never re-runs
    # pr_table, so the table comment AND the body count would both go stale. Refresh the
    # trimmed table, then sync the body to the KNOWN green count (passed explicitly, so no
    # `gh pr diff` and no GitHub diff-recompute lag right after the force-push).
    import pr_table
    sh([sys.executable, str(HERE / "pr_table.py"), str(pr), "--repo", REPO, "--post", "--no-body-sync"], check=False)
    pr_table.sync_body_count(pr, REPO, n=len(green["tags"]))


def do_open(mathlib, dry):
    print("  OPEN next batch (open_batch.py)")
    # check=True: a failed open (open_batch.py already sys.exit(1)s on a crash/build
    # fail) must FAIL the tick — not silently report success while current_pr never
    # advances (the bug that masked the Lean-core FileNotFoundError).
    sh([sys.executable, str(HERE / "open_batch.py"), "--mathlib", str(mathlib)] + ([] if dry else ["--apply"]), check=True)


def ci_cache_flake(pr, repo):
    """The fork-CI cache-replay flake: 'Post-Build Step' failed while 'Build'
    passed — lake 'target is out-of-date' at cache replay, on core files the PR
    never touched. A close+reopen re-runs CI against a warmer cache and clears it
    (batch #40682 passed Post-Build with the same files; #40747 flaked)."""
    out = subprocess.run(["gh", "pr", "checks", str(pr), "--repo", repo],
                         capture_output=True, text=True).stdout
    rows = [l.split("\t") for l in out.splitlines() if "\t" in l]
    pb = [r[1] for r in rows if len(r) > 1 and "Post-Build Step" in r[0]]
    build = [r[1] for r in rows if len(r) > 1 and "/ Build" in r[0]]
    return ("fail" in pb) and ("pass" in build) and ("fail" not in build)


def retrigger(pr, repo):
    """Re-run a fork PR's CI without a noise commit: close then reopen (the
    'reopened' event re-fires the workflow). A fork author can do this; they
    can't hit 'Re-run' on the upstream Actions UI."""
    sh(["gh", "pr", "close", str(pr), "--repo", repo])
    sh(["gh", "pr", "reopen", str(pr), "--repo", repo])


def tick(mathlib, dry, no_open=False):
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    pr, branch = st.get("current_pr"), st.get("branch")
    if not pr:
        print("no current PR in state — opening first batch")
        if not no_open:
            do_open(mathlib, dry)
        return
    state = gh_state(pr)
    merged = settle.is_merged(pr, REPO)  # bors closes the PR, so check title/merged too
    print(f"poll #{pr}: state={state} merged={merged}  settled={st.get('settled_pr') == pr}")
    if merged:
        # Auto-populate Wikidata P14534 from the freshly-merged @[wikidata] tags
        # (the merge IS the review gate). Gated + best-effort — never blocks the
        # batch-open. Runs at most ONCE per merged PR (pushed_pr guard).
        push_property_on_merge(mathlib, dry, pr, st)
        if no_open:
            print("  MERGED ✓ — open the next batch (supervised): "
                  "`poll.py --apply` without --no-open, or open_batch.py --apply"); return
        do_open(mathlib, dry)
        if not dry:  # open_batch advanced current_pr; clear the stale markers
            st = json.loads(STATE.read_text())
            st.pop("settled_pr", None); st.pop("retriggered_pr", None)
            STATE.write_text(json.dumps(st, indent=1))
        return
    if state != "OPEN":
        print(f"  #{pr} is {state} but NOT merged — needs manual attention; skipping"); return
    if st.get("settled_pr") == pr:
        # Reviews can land AFTER the settle (e.g. a maintainer rejects a tag that
        # was green). Re-classify against the PR's CURRENT tags: if any are now
        # recycled, re-settle to trim them. Once trimmed the recycle is empty, so
        # this converges (idempotent). Re-trim takes precedence over CI self-heal.
        recls = settle.classify(pr, REPO)
        if recls["gate"] and recls["recycle"]:
            print(f"  RE-SETTLE #{pr}: {len(recls['recycle'])} tag(s) rejected since the "
                  f"last settle — re-trimming to {len(recls['green'])} green")
            do_settle(pr, branch, mathlib, recls, dry)
            if not dry:
                st["settled_pr"] = pr; st.pop("retriggered_pr", None)  # fresh commit → CI re-runs
                STATE.write_text(json.dumps(st, indent=1))
            return
        # Before the CI self-heal: auto-resolve a master conflict. An additive
        # @[wikidata] PR that fell behind master is RE-DERIVED off fresh master
        # (conflict-proof — text-merging master in is what kept conflicting) and
        # force-pushed. Guarded by the master tip we last rebuilt onto so a
        # non-resolving rebuild can't loop.
        if pr_conflicting(pr) and st.get("conflict_sha") != master_tip():
            print(f"  #{pr} CONFLICTING with master — rebuilding off fresh master")
            resolve_conflicts(pr, branch, mathlib, dry)   # records conflict_sha itself (loop-guard)
            return
        # No new trims — self-heal the fork-CI cache-verify failure (Post-Build
        # 'target is out-of-date'): the proven fix is merging current master into
        # the branch — a stale merged-master leaves core-file oleans uncached —
        # and the push re-runs CI. If ALREADY current it's a genuine flake, so
        # fall back to a close+reopen re-trigger. At most ONCE per PR.
        if st.get("retriggered_pr") != pr and ci_cache_flake(pr, REPO):
            print("  Post-Build cache failure — freshening branch against master")
            if dry:
                print(f"    [dry-run] would merge master into {branch} + push (else close+reopen)")
            else:
                if split.freshen_branch_and_push(branch, mathlib):
                    print("    pushed master-merge — CI re-runs")
                else:
                    print("    already current — re-triggering via close+reopen")
                    retrigger(pr, REPO)
                st["retriggered_pr"] = pr
                STATE.write_text(json.dumps(st, indent=1))
        else:
            print("  already settled — waiting for merge")
        return
    cls = settle.classify(pr, REPO)
    if not cls["gate"]:
        print(f"  gate not met ({cls['gate_reasons']}) — waiting for reviews"); return
    do_settle(pr, branch, mathlib, cls, dry)
    if not dry:
        st["settled_pr"] = pr; STATE.write_text(json.dumps(st, indent=1))


def push_property_on_merge(mathlib, dry, pr, st):
    """Auto-populate Wikidata P14534 from the just-merged @[wikidata] tags.
    Gated by WIKILEAN_PUSH_PROPERTY=1 (QUICKSTATEMENTS_TOKEN is checked inside
    push_property.py, which stays dry-run without it). Regenerates the seed from
    fresh master, then submits ONLY net-new statements (diffed live vs Wikidata;
    conflicts and catalog-settled tiebreaks are excluded).

    ONCE per merged PR (pushed_pr guard): the merged block re-enters on later
    ticks — the --no-open supervised window, or a do_open failure that sys.exits
    AFTER this ran — and under WDQS replication lag the value-only diff would
    re-submit the just-merged batch, appending duplicate reference blocks. So we
    push at most once per merged PR, marking pushed_pr only on a clean (exit-0)
    run; a QS error / WDQS-partial abort (non-zero) leaves it unset to retry.
    Best-effort: sh is unchecked, so a stumble never fails the tick."""
    if os.environ.get("WIKILEAN_PUSH_PROPERTY") != "1":
        return
    if st.get("pushed_pr") == pr:
        print(f"  push_property: already pushed for merged #{pr} — skip")
        return
    print("  push_property: refresh P14534 seed from merged master + submit net-new")
    sh([sys.executable, str(HERE / "export_property_seed.py"), "--mathlib", str(mathlib)])
    r = sh([sys.executable, str(HERE / "push_property.py")] + ([] if dry else ["--submit"]))
    if not dry and getattr(r, "returncode", 1) == 0:
        st["pushed_pr"] = pr
        STATE.write_text(json.dumps(st, indent=1))


def decide():
    """Cheap, GitHub-only verdict — would this tick ACT (open/settle/self-heal)
    or WAIT? Mirrors tick()'s branch logic but touches no filesystem/mathlib, so
    CI can run it first and skip the heavy elan/lake/claude/clone setup on the
    (vast majority) no-op ticks. Returns 'act' | 'wait'."""
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    pr = st.get("current_pr")
    if not pr:
        return "act"                                   # open the first batch
    if settle.is_merged(pr, REPO):
        return "act"                                   # merged -> open next
    if gh_state(pr) != "OPEN":
        return "wait"                                  # closed-not-merged -> manual
    if st.get("settled_pr") == pr:                     # settled -> re-trim new rejections, else CI self-heal
        cls = settle.classify(pr, REPO)
        if cls["gate"] and cls["recycle"]:
            return "act"                               # a tag was rejected after the settle -> re-trim
        if pr_conflicting(pr) and st.get("conflict_sha") != master_tip():
            return "act"                               # PR conflicts with master -> rebuild off master
        return "act" if (st.get("retriggered_pr") != pr and ci_cache_flake(pr, REPO)) else "wait"
    return "act" if settle.classify(pr, REPO)["gate"] else "wait"   # gate met -> settle


def refresh_table():
    """Cheap (no mathlib/lean): re-post the current OPEN, un-settled PR's tag table so
    its Reviews column reflects verdicts that landed since it was opened — pr_table
    skips the write when nothing changed, so this is safe to run every tick. Settled
    PRs already carry the green-only table (re-posted by the settle), so leave them be.
    Fixes the table going stale between open and settle (it only refreshed at those
    two points, so a reviewer's verdicts never appeared until the gate was met)."""
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    pr = st.get("current_pr")
    if not pr or gh_state(pr) != "OPEN" or st.get("settled_pr") == pr:
        return
    sh([sys.executable, str(HERE / "pr_table.py"), str(pr), "--repo", REPO, "--post", "--fresh"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--watch", type=int, default=0, help="poll every N seconds (0 = one tick)")
    ap.add_argument("--no-open", action="store_true",
                    help="on merge, alert instead of auto-opening the next batch (supervise the first open)")
    ap.add_argument("--decide", action="store_true",
                    help="print POLL_DECISION=act|wait (gh-only, no mathlib) and exit; CI gate")
    ap.add_argument("--refresh-table", action="store_true",
                    help="cheap: re-post the current OPEN PR's tag table to reflect new reviews, then exit")
    args = ap.parse_args()
    if args.decide:
        print(f"POLL_DECISION={decide()}")
        return
    if args.refresh_table:
        refresh_table()
        return
    dry = not args.apply
    while True:
        tick(args.mathlib, dry, args.no_open)
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
