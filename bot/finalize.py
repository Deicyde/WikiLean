#!/usr/bin/env python3
"""Finalize a freshly-opened @[wikidata] batch PR — the steps open_batch.py used
to only PRINT as "supervised". Each step is BEST-EFFORT (logged, never fatal):
the PR is already open and bot_state already advanced, so a finalize hiccup must
not crash the run or block the other steps. ALWAYS exits 0.

  finalize.py --pr N --mathlib <checkout> --approved batchN_approved.json

Steps:
  1. LLM-generated label — REQUIRED on every WikiLean→Mathlib PR. open_batch_pr.py
     --llm-label posts the `LLM-generated` trigger comment; the upstream
     github-actions bot then applies the label.
  2. crossref inline comments — one inline comment per @[wikidata] tag, via the
     patched Deicyde/mathlib-crossref-report (brew-bash + coreutils gnubin +
     CROSSREF_REPO=fork + CROSSREF_DIFF_FILE=a pre-fetched diff, since the
     patch-diff.githubusercontent.com URL is unreachable from this host).
  3. /queue publish — recycled + a fresh pool sample to the wiki /queue page.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATE = HERE / "state"
REPO = "leanprover-community/mathlib4"
FORK = "Deicyde/mathlib4"
CROSSREF = (Path(os.environ["WIKILEAN_CROSSREF_DIR"]) if os.environ.get("WIKILEAN_CROSSREF_DIR")
            else Path.home() / "Desktop" / "LEAN" / "mathlib-crossref-report")  # macOS default
CROSSREF_GIT = "https://github.com/Deicyde/mathlib-crossref-report.git"
# macOS needs brew-bash (4+) + GNU coreutils on PATH; a Linux CI runner has both
# natively, so BASH=bash and no gnubin prefix. Both env-overridable.
_MAC = sys.platform == "darwin"
BREW_BASH = os.environ.get("WIKILEAN_BASH") or ("/opt/homebrew/bin/bash" if _MAC else "bash")
GNUBIN = os.environ.get("WIKILEAN_GNUBIN", "/opt/homebrew/opt/coreutils/libexec/gnubin" if _MAC else "")


def run(cmd, **kw):
    print("    $", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, text=True, **kw)


def step_label(pr, mathlib, approved_path):
    print("  [1/3] LLM-generated label", flush=True)
    # --llm-label reads --approved unconditionally, so all four args are required.
    r = run([sys.executable, str(HERE / "open_batch_pr.py"), "--llm-label",
             "--pr", str(pr), "--approved", str(approved_path),
             "--mathlib", str(mathlib), "--repo", REPO], check=False)
    return r.returncode == 0


def step_crossref(pr):
    print("  [2/3] crossref inline comments", flush=True)
    script = CROSSREF / "scripts" / "post-wikidata-comments.sh"
    if not script.exists():
        CROSSREF.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", CROSSREF_GIT, str(CROSSREF)], check=False)
    if not script.exists():
        print("    (crossref tool unavailable — skipped)"); return False
    # Pre-fetch the diff (the curl path to patch-diff is unreachable here).
    diff = Path(f"/tmp/pr{pr}.diff")
    d = run(["gh", "pr", "diff", str(pr), "--repo", REPO], check=False, capture_output=True)
    if d.returncode != 0 or not (d.stdout or "").strip():
        print("    (could not fetch PR diff — skipped)"); return False
    diff.write_text(d.stdout)
    path = (GNUBIN + ":" + os.environ.get("PATH", "")) if GNUBIN else os.environ.get("PATH", "")
    env = {**os.environ, "PATH": path, "CROSSREF_REPO": FORK, "CROSSREF_DIFF_FILE": str(diff)}
    ok = True
    for s in ("warm-cache.sh", "post-wikidata-comments.sh"):
        r = run([BREW_BASH, f"./scripts/{s}", str(pr)], cwd=str(CROSSREF), env=env, check=False)
        ok = ok and (r.returncode == 0)
    return ok


def step_queue(pr, approved):
    print("  [3/3] /queue publish", flush=True)
    import pool  # bot/ on sys.path
    batch_qids = {t["qid"] for t in (approved or {}).get("tags", [])}
    recycle = STATE / "recycle_queue.json"
    rq = {e["qid"] for e in json.loads(recycle.read_text())} if recycle.exists() else set()
    fresh = pool.candidates(20, exclude=batch_qids | rq)
    cand = STATE / "pool_candidates.json"
    cand.write_text(json.dumps(fresh))
    cmd = [sys.executable, str(HERE / "publish_queue.py"), "--candidates", str(cand)]
    if recycle.exists():
        cmd += ["--recycle", str(recycle)]
    return run(cmd, check=False).returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--approved", type=Path)
    args = ap.parse_args()
    approved = (json.loads(args.approved.read_text())
                if args.approved and args.approved.exists() else None)
    print(f"finalizing #{args.pr} (best-effort)…", flush=True)
    res = {}
    for name, fn in (("label", lambda: step_label(args.pr, args.mathlib, args.approved)),
                     ("crossref", lambda: step_crossref(args.pr)),
                     ("queue", lambda: step_queue(args.pr, approved))):
        try:
            res[name] = fn()
        except Exception as e:
            print(f"  {name} raised: {type(e).__name__}: {e}")
            res[name] = False
    print("finalize #%d: %s" % (args.pr,
          "  ".join(f"{k}={'ok' if v else 'FAILED'}" for k, v in res.items())), flush=True)
    # ALWAYS exit 0 — best-effort; the open already succeeded.


if __name__ == "__main__":
    main()
