#!/usr/bin/env python3
"""Export the merged @[wikidata] tags as the human-reviewed SEED batch for the
Wikidata "Mathlib declaration" property proposal (docs/wikidata_property_proposal.md).

Reframing that unblocks the proposal: instead of "human-review ~815 AI mappings",
the ~127 tags ALREADY MERGED into mathlib master each passed the bot's review gate
(≥2 human reviewers incl. ≥1 maintainer) plus mathlib CI — they ARE the reviewed
seed. This script joins them with their provenance and emits sign-off artifacts.
Jack still personally reviews the output before anything is submitted to Wikidata
(standing rule; this script only WRITES LOCAL FILES).

Pipeline (deterministic; network = git fetch + gh pr diff only):
  1. `git grep` upstream master (FETCH_HEAD after a shallow fetch) for every
     `wikidata Qxxx` attribute → the live QID set (must equal tagged_in_master.txt).
  2. QID → decl from the catalog (tag_qid side). Every (QID, decl) is then
     VERIFIED against the master source: the attribute line's following
     declaration must name-match the catalog decl, or the row is flagged.
  3. Batch attribution from the merged bot PR diffs (#40440 #40682 #40747
     #40861 #40970); QIDs added by none of them = external (organic adopters).

Outputs (bot/data/, gitable):
  property_seed.tsv                 QID · label · decl · module · batch · PR · status
  property_seed_quickstatements.csv QS v1 lines with a PXXXX placeholder (replace
                                    with the assigned property id after creation)
  property_seed_flags.tsv           rows needing hand review (mismatch/no-decl)

  python3 bot/export_property_seed.py [--mathlib /Users/jack/Desktop/LEAN/mathlib4]
      [--offline]   # skip gh pr diffs (batch attribution falls back to state files)
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "bot" / "data"
STATE = ROOT / "bot" / "state"
UPSTREAM = "https://github.com/leanprover-community/mathlib4"
# batch → merged PR (resolved from gh by head branch wikilean/wikidata-batch-N;
# batches 1-5 merged via Bors, batch 6 open as of 2026-07-01 and NOT in master).
BATCH_PRS = {1: 40440, 2: 40682, 3: 40747, 4: 40861, 5: 40970}
TAG_RE = re.compile(r"wikidata\s+(Q\d+)")
# The declaration line that follows an attribute: optional modifiers, then a
# decl keyword and name. Name may be namespaced or «quoted».
DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:public|protected|private|noncomputable|partial|unsafe|scoped|nonrec)\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque|irreducible_def)\s+"
    r"([A-Za-z0-9_.«»'ₐ-ₜ₀-₉!?]+)")
# Tags applied AFTER the declaration via an attribute statement — the target
# name sits at the end of the same line:
#   attribute [to_additive (attr := wikidata Q83478)] Group
ATTR_STMT_RE = re.compile(r"^\s*attribute\s+\[.*\]\s+([A-Za-z0-9_.«»'ₐ-ₜ₀-₉!?]+)\s*$")


def sh(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=180)
    if r.returncode != 0:
        sys.exit(f"command failed ({' '.join(args[:4])}…): {r.stderr[:300]}")
    return r.stdout


def master_tags(mathlib: Path) -> dict[str, tuple[str, int]]:
    """QID → (file, line) of its @[wikidata] attribute on upstream master."""
    sh(["git", "-C", str(mathlib), "fetch", "--depth=1", UPSTREAM, "master"])
    out = subprocess.run(
        ["git", "-C", str(mathlib), "grep", "-nIE", r"wikidata[[:space:]]+Q[0-9]+",
         "FETCH_HEAD", "--", "Mathlib/"],
        text=True, capture_output=True).stdout
    tags: dict[str, tuple[str, int]] = {}
    for line in out.splitlines():
        # FETCH_HEAD:Mathlib/Foo/Bar.lean:123:@[wikidata Q42] …
        m = re.match(r"^FETCH_HEAD:([^:]+):(\d+):(.*)$", line)
        if not m:
            continue
        path, lineno, text = m.group(1), int(m.group(2)), m.group(3)
        if path.endswith("CrossRefAttribute.lean"):
            continue  # the attribute's own docs, not a tag
        # One line may carry several tags: @[wikidata Q12916, wikidata Q2584477]
        for q in TAG_RE.finditer(text):
            tags.setdefault(q.group(1), (path, lineno))
    return tags


def decl_after(mathlib: Path, path: str, lineno: int) -> str | None:
    """The (possibly unqualified) decl name the tag at path:lineno is attached to."""
    src = sh(["git", "-C", str(mathlib), "show", f"FETCH_HEAD:{path}"])
    lines = src.splitlines()
    # attribute-statement form: the target is on the tag line itself.
    m = ATTR_STMT_RE.match(lines[lineno - 1])
    if m:
        return m.group(1)
    for i in range(lineno - 1, min(lineno + 14, len(lines))):
        m = DECL_RE.match(lines[i])
        if m:
            return m.group(1)
    return None


def load_decl_index() -> dict[str, str]:
    """decl → module from the wiki's doc-gen4 decl-index shards (411k decls)."""
    idx_dir = ROOT / "wiki" / "public" / "assets" / "decl-index"
    idx: dict[str, str] = {}
    for shard in idx_dir.glob("*.json"):
        if shard.name != "manifest.json":
            for decl, module in json.loads(shard.read_text()):
                idx[decl] = module
    return idx


def qualify(name: str, file_module: str, idx: dict[str, str]) -> tuple[str, str] | None:
    """Fully qualify a source-extracted decl name against the decl-index.
    Prefer an in-module match (exact, then unique dotted suffix); fall back to a
    unique match anywhere. Returns (fully_qualified, module) or None."""
    if name in idx and idx[name] == file_module:
        return name, idx[name]
    in_module = [d for d, m in idx.items() if m == file_module
                 and (d == name or d.endswith("." + name))]
    if len(in_module) == 1:
        return in_module[0], file_module
    if name in idx:  # exact name, different module (file moved since docs build)
        return name, idx[name]
    anywhere = [d for d in idx if d.endswith("." + name)]
    if len(anywhere) == 1:
        return anywhere[0], idx[anywhere[0]]
    return None


def catalog_decls() -> dict[str, dict]:
    """tag_qid → {decl, module, label} from every catalog file (later wins)."""
    files = ["pilot_tagged.jsonl", "tier2_tagged.jsonl", "generated_candidates.jsonl",
             "mathlib_yaml_tagged.jsonl", "refresh_tagged.jsonl"]
    out: dict[str, dict] = {}
    for name in files:
        p = ROOT / "catalog" / "data" / name
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            pd = r.get("primary_decl")
            if not pd:
                continue
            tq = r.get("primary_qid") or r.get("wikidata_qid")
            module = next((d.get("module") for d in r.get("mathlib_decls", [])
                           if d.get("decl") == pd), None)
            label = r.get("primary_qid_label") or r.get("title") or ""
            if isinstance(tq, str):
                out[tq] = {"decl": pd, "module": module, "label": label}
    return out


def batch_attribution(offline: bool) -> dict[str, int]:
    """QID → batch number. PR diffs are authoritative (they show exactly what
    merged); state files are the offline fallback (what was proposed)."""
    attr: dict[str, int] = {}
    if not offline:
        for batch, pr in BATCH_PRS.items():
            diff = subprocess.run(
                ["gh", "pr", "diff", str(pr), "--repo", "leanprover-community/mathlib4"],
                text=True, capture_output=True, timeout=120).stdout
            for line in diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    for q in TAG_RE.findall(line):
                        attr.setdefault(q, batch)
        if attr:
            return attr
        print("warn: no PR diffs readable — falling back to state files", file=sys.stderr)
    for f in sorted(STATE.glob("batch*_approved.json")):
        d = json.loads(f.read_text())
        for t in d.get("tags", []):
            if isinstance(t.get("qid"), str):
                attr.setdefault(t["qid"], d.get("batch"))
    return attr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, default=Path("/Users/jack/Desktop/LEAN/mathlib4"))
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    tags = master_tags(args.mathlib)
    known = {l.strip() for l in (DATA / "tagged_in_master.txt").read_text().splitlines() if l.strip()}
    if set(tags) != known:
        print(f"note: master grep ({len(tags)}) != tagged_in_master.txt ({len(known)}) — "
              f"using the live grep; run refresh_tagged.py to sync", file=sys.stderr)

    cat = catalog_decls()
    batches = batch_attribution(args.offline)
    idx = load_decl_index()

    rows, flags = [], []
    for qid in sorted(tags, key=lambda q: int(q[1:])):
        path, lineno = tags[qid]
        src_decl = decl_after(args.mathlib, path, lineno)
        c = cat.get(qid)
        batch = batches.get(qid)
        pr = BATCH_PRS.get(batch) if batch else None
        file_module = path.removesuffix(".lean").replace("/", ".")
        # The source of truth is master's own tag site: extract the decl name,
        # fully qualify it against the doc-gen4 decl-index. The catalog only
        # corroborates; a disagreement is flagged but source wins.
        q = qualify(src_decl, file_module, idx) if src_decl else None
        if q:
            decl, module = q
            status = "bot" if batch else "external"
            if c and c["decl"] != decl:
                status += "+catalog-mismatch"
                flags.append((qid, path, lineno, decl, c["decl"]))
        elif c:
            decl, module = c["decl"], c.get("module") or file_module
            status = "catalog-only(unverified)"
            flags.append((qid, path, lineno, src_decl or "—", c["decl"]))
        else:
            decl, module, status = src_decl or "?", file_module, "unresolved"
            flags.append((qid, path, lineno, src_decl or "—", "—"))
        rows.append({"qid": qid, "label": (c or {}).get("label") or "",
                     "decl": decl, "module": module, "batch": batch or "",
                     "pr": f"{UPSTREAM}/pull/{pr}" if pr else "", "status": status})

    tsv = ["qid\tlabel\tdecl\tmodule\tbatch\tpr\tstatus"]
    tsv += [f"{r['qid']}\t{r['label']}\t{r['decl']}\t{r['module']}\t{r['batch']}\t{r['pr']}\t{r['status']}"
            for r in rows]
    (DATA / "property_seed.tsv").write_text("\n".join(tsv) + "\n")

    qs = ["# QuickStatements v1 — REPLACE PXXXX with the property id once created.",
          "# Value = fully-qualified Mathlib declaration name (proposal Option A).",
          "# Source: @[wikidata] tags merged into leanprover-community/mathlib4 master,",
          "# each reviewed by >=2 humans incl. a maintainer (PR provenance in property_seed.tsv)."]
    qs += [f'{r["qid"]}\tPXXXX\t"{r["decl"]}"' for r in rows
           if r["status"] in ("bot", "external") or r["status"].endswith("+catalog-mismatch")]
    (DATA / "property_seed_quickstatements.csv").write_text("\n".join(qs) + "\n")

    if flags:
        ftsv = ["qid\tfile\tline\tsource_decl\tcatalog_decl"]
        ftsv += [f"{q}\t{p}\t{ln}\t{s}\t{c}" for q, p, ln, s, c in flags]
        (DATA / "property_seed_flags.tsv").write_text("\n".join(ftsv) + "\n")

    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    n_qs = sum(1 for r in rows
               if r["status"] in ("bot", "external") or r["status"].endswith("+catalog-mismatch"))
    print(f"{len(rows)} merged tags → property_seed.tsv  ({by})")
    print(f"QuickStatements rows: {n_qs}  |  flagged for hand review: {len(flags)}")
    print("NEXT: Jack reviews property_seed.tsv before ANY Wikidata submission.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
