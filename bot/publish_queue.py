#!/usr/bin/env python3
"""Publish the tag queue to the wiki's /api/queue (bot bearer token).

Sources (any/all):
  --payload FILE       a ready {"items":[…]} blob (e.g. state/seed_queue.json)
  --recycle FILE       triage output (recycle_queue.json) -> recycled items
  --candidates FILE    [{qid,label,decl,file}] -> unreviewed items
Auth: --token or $PIPELINE_TOKEN. --dry-run prints the payload instead of POSTing.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

DEV_VARS = Path(__file__).resolve().parent.parent / "wiki" / ".dev.vars"


def find_token():
    """WIKILEAN_API_TOKEN env, else PIPELINE_TOKEN= from wiki/.dev.vars (as moderate.py)."""
    tok = os.environ.get("WIKILEAN_API_TOKEN") or os.environ.get("PIPELINE_TOKEN")
    if tok:
        return tok
    if DEV_VARS.exists():
        for line in DEV_VARS.read_text().splitlines():
            if line.startswith("PIPELINE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", default="https://wikilean.jackmccarthy.org")
    ap.add_argument("--payload", type=Path)
    ap.add_argument("--recycle", type=Path)
    ap.add_argument("--candidates", type=Path)
    ap.add_argument("--token", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    token = args.token or find_token()

    items = []
    if args.payload and args.payload.exists():
        items += json.loads(args.payload.read_text()).get("items", [])
    if args.recycle and args.recycle.exists():
        for e in json.loads(args.recycle.read_text()):
            t = e.get("triage", {})
            fix = t.get("fix_hint", "")
            items.append({
                "qid": e["qid"],
                "decl": t.get("suggested_decl") or e.get("current_decl", ""),
                "file": e.get("file"),
                "status": "recycled",
                "notes": e.get("notes", []),
                "retarget": (((t.get("suggested_decl") or "") + (" — " + fix if fix else "")).strip(" —")),
            })
    if args.candidates and args.candidates.exists():
        for c in json.loads(args.candidates.read_text()):
            items.append({"qid": c["qid"], "label": c.get("label"), "decl": c.get("decl", ""),
                          "file": c.get("file"), "status": "unreviewed"})

    payload = {"items": items}
    if args.dry_run:
        print(json.dumps(payload, indent=1)); return
    if not token:
        sys.exit("no token: set WIKILEAN_API_TOKEN, pass --token, or add PIPELINE_TOKEN= to wiki/.dev.vars")
    r = subprocess.run(["curl", "-sS", "-X", "POST", args.wiki + "/api/queue",
                        "-H", "Authorization: Bearer " + token,
                        "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
                       capture_output=True, text=True)
    print(r.stdout or r.stderr)


if __name__ == "__main__":
    main()
