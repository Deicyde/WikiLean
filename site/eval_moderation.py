#!/usr/bin/env python3
"""Moderation-behavior eval suite for WikiLean (deterministic, zero tokens).

Each fixture in site/eval_fixtures/ plants a known adversarial-or-benign agent
output for a tiny synthetic article and pins what the DETERMINISTIC machinery
must do with it:

    (a) drop_human            dropped human re-inserted, id intact
    (b) tombstone_resurrect   exact-sig resurrection suppressed; the
                              shifted-anchor near-miss now dropped at the
                              wire (F6 veto-adjacency)
    (c) rename_ids            unknown ids discarded, stored ids inherited by sig
    (d) provenance_downgrade  PRIORITY ladder blocks, downgrades_blocked counted
    (e) coverage_extend       fresh ids minted, every tamper-stat zero
    (f) create_launder        create path downgrades minted 'human' provenance
    (g) human_reanchor        agent re-anchors a human annotation → id-match
                              replaces in place, exactly ONE copy survives
                              (F7); attached moderation_flag harvested (F14)

--offline (the CI gate) imports the REAL functions and runs the REAL
batch_annotate.annotate_one moderation flow — fetch/extract/render and the two
run_agent calls are replaced by planted fixture outputs, so the inline
_preserve_human passes and the PRIORITY ladder execute exactly as in
production, then moderate.finalize_for_post builds the wire payload. No
network, no agents, no tokens. Exit code is non-zero on any failed
expectation.

--live is a STUB (wiring written + arg-gated, deliberately never executed in
CI): it runs the REAL agents over the same synthetic fixtures and scores the
same human-safety expectations plus coverage/decl-sanity heuristics. It is
double-gated: --live requires --i-understand-this-costs-tokens.

Usage:
    python3 site/eval_moderation.py --offline
    python3 site/eval_moderation.py --offline --only drop_human --verbose
    python3 site/eval_moderation.py --offline --require-all   # skips fail too
    python3 site/eval_moderation.py --live --i-understand-this-costs-tokens

batch_annotate (and its claude-agent-sdk import) is required for the five
moderate-pipeline scenarios; without it they SKIP (create_launder still runs).
Use --require-all to turn skips into failures (recommended for CI on a
machine with the venv: catalog/.venv/bin/python site/eval_moderation.py
--offline --require-all).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import moderate as m  # noqa: E402  (stdlib-only at import time)

try:
    import batch_annotate as ba
except ImportError as _e:  # pragma: no cover - environment-dependent
    ba = None
    _BA_ERR = str(_e)

FIXTURES_DIR = HERE / "eval_fixtures"
HEX12_RE = re.compile(r"^[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# Harness: run the REAL annotate_one moderation flow over planted agent output
# ---------------------------------------------------------------------------

def run_moderate_pipeline(fix: dict, live: bool = False) -> tuple[dict, list]:
    """Execute batch_annotate.annotate_one(moderate=True) with the fixture's
    article + existing annotations. Offline: run_agent is replaced by a queue
    of the two planted outputs; fetch/extract/render are stubbed (the article
    is synthetic). The inline deterministic machinery — both _preserve_human
    passes and the PRIORITY ladder — is the REAL production code.

    Live (STUB): identical, except run_agent stays real, so the actual agents
    moderate the synthetic article. Costs tokens; never run in CI.
    """
    title = fix["title"]
    slug = ba.make_slug(title)
    tmp = Path(tempfile.mkdtemp(prefix="wl-eval-"))
    cache, annot, out = tmp / "cache", tmp / "annotations", tmp / "out"
    for d in (cache, annot, out):
        d.mkdir(parents=True)

    queue = [copy.deepcopy(fix["agent1_output"]), copy.deepcopy(fix["agent2_output"])]

    async def planted_agent(system, user, cwd, tools, max_turns, mcp_servers=None):
        return queue.pop(0), {"n_tool_calls": 0, "tools_used": {}, "cost_usd_equiv": 0.0,
                              "tokens": 0, "duration_ms": 1}

    def planted_script(script: str, slug_: str) -> tuple[int, str]:
        if script == "render.py":
            (ba.OUT / f"{slug_}.html").write_text("<!-- eval stub -->", encoding="utf-8")
            return 0, "eval-stub: 1/1 matched"
        if script == "validate_coverage.py":
            return 0, "Coverage: 1/1 statements covered (100%)"
        return 0, ""  # extract_sections.py — sections.json is pre-planted

    patched = ("CACHE", "ANNOT", "OUT", "fetch_html", "run_script", "run_agent")
    saved = {k: getattr(ba, k) for k in patched}
    try:
        ba.CACHE, ba.ANNOT, ba.OUT = cache, annot, out
        # target_revid kwarg added by fix F1 (evals run the legacy/None path)
        ba.fetch_html = lambda s, t, target_revid=None: True
        ba.run_script = planted_script
        if not live:
            ba.run_agent = planted_agent
        (cache / f"{slug}.sections.json").write_text(
            json.dumps(fix["sections"], ensure_ascii=False), encoding="utf-8")
        rec = asyncio.run(ba.annotate_one(
            {"title": title}, asyncio.Semaphore(1), {},
            moderate=True, existing_override=copy.deepcopy(fix["existing"])))
        final = []
        if not rec.get("error"):
            final = json.loads((annot / f"{slug}.json").read_text(
                encoding="utf-8"))["annotations"]
    finally:
        for k, v in saved.items():
            setattr(ba, k, v)
        shutil.rmtree(tmp, ignore_errors=True)
    return rec, final


# ---------------------------------------------------------------------------
# Checks engine
# ---------------------------------------------------------------------------

def _by_label(posted: list, label: str) -> dict | None:
    hits = [a for a in posted if a.get("label") == label]
    return hits[0] if len(hits) == 1 else None


def run_check(chk: dict, fix: dict, posted: list) -> tuple[bool, str]:
    kind = chk["check"]
    existing = fix.get("existing", [])
    if kind == "count":
        ok = len(posted) == chk["n"]
        return ok, f"count {len(posted)} == {chk['n']}"
    if kind == "human_verbatim":
        stored = next((a for a in existing if a.get("id") == chk["id"]), None)
        ok = stored is not None and stored in posted
        return ok, f"stored human {chk['id']} posted byte-identical"
    if kind == "humans_intact":
        # py approximation of the server's findLostHuman: every stored human
        # (tombstones included) appears in the wire payload deep-equal.
        missing = [h.get("label") or m._anchor_sig(h)
                   for h in existing
                   if h.get("provenance") == "human" and h not in posted]
        return not missing, f"no stored human lost/altered (missing={missing})"
    if kind == "label_provenance":
        a = _by_label(posted, chk["label"])
        ok = a is not None and a.get("provenance") == chk["provenance"]
        return ok, f"{chk['label']!r} provenance == {chk['provenance']!r}"
    if kind == "label_status":
        a = _by_label(posted, chk["label"])
        ok = a is not None and a.get("status") == chk["status"]
        return ok, f"{chk['label']!r} status == {chk['status']!r}"
    if kind == "label_id":
        a = _by_label(posted, chk["label"])
        ok = a is not None and a.get("id") == chk["id"]
        return ok, f"{chk['label']!r} id == {chk['id']}"
    if kind == "label_fresh_id":
        a = _by_label(posted, chk["label"])
        known = {x.get("id") for x in existing if isinstance(x.get("id"), str)}
        aid = a.get("id") if a else None
        ok = (isinstance(aid, str) and bool(HEX12_RE.match(aid))
              and aid not in known)
        return ok, f"{chk['label']!r} got a fresh 12-hex id ({aid})"
    if kind == "label_present":
        ok = _by_label(posted, chk["label"]) is not None
        note = f" — {chk['note']}" if chk.get("note") else ""
        return ok, f"{chk['label']!r} present{note}"
    if kind == "label_absent":
        ok = all(a.get("label") != chk["label"] for a in posted)
        return ok, f"{chk['label']!r} absent"
    if kind == "id_absent":
        ok = all(a.get("id") != chk["id"] for a in posted)
        return ok, f"id {chk['id']} absent from the wire payload"
    if kind == "all_ids_unique_hex":
        ids = [a.get("id") for a in posted]
        ok = (all(isinstance(i, str) and HEX12_RE.match(i) for i in ids)
              and len(set(ids)) == len(ids))
        return ok, "every posted id is unique 12-hex"
    return False, f"unknown check kind {kind!r}"


def dict_diff(got: dict, want: dict) -> str:
    keys = sorted(set(got) | set(want))
    return ", ".join(f"{k}: got {got.get(k)} want {want.get(k)}"
                     for k in keys if got.get(k) != want.get(k))


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------

def eval_offline_scenario(fix: dict, verbose: bool) -> dict:
    """Returns {name, status: PASS|FAIL|SKIP, results: [(ok, msg)], notes}."""
    results: list[tuple[bool, str]] = []
    ladder = wire = None

    if fix.get("pipeline") == "create":
        body, wire = m.build_create_body(
            copy.deepcopy(fix["envelope"]),
            revid=fix.get("revid"), wikidata_qid=fix.get("wikidata_qid"),
            run_id=fix.get("run_id", "evalrun0"))
        posted = body["annotations"]
        for field, want in fix["expect"].get("body", {}).items():
            ok = body.get(field) == want
            results.append((ok, f"body[{field!r}] == {want!r}"
                                + ("" if ok else f" (got {body.get(field)!r})")))
    else:
        if ba is None:
            return {"name": fix["name"], "status": "SKIP", "results": [],
                    "notes": [f"batch_annotate unavailable ({_BA_ERR}) — run "
                              f"under catalog/.venv for this scenario"]}
        rec, final = run_moderate_pipeline(fix)
        if rec.get("error"):
            return {"name": fix["name"], "status": "FAIL",
                    "results": [(False, f"pipeline error: {rec['error']}")],
                    "notes": fix.get("notes", [])}
        ladder = rec.get("ladder")
        want_ladder = fix["expect"]["ladder"]
        ok = ladder == want_ladder
        results.append((ok, "ladder == " + json.dumps(want_ladder)
                            + ("" if ok else f" (diff: {dict_diff(ladder, want_ladder)})")))
        posted, wire = m.finalize_for_post(copy.deepcopy(fix["existing"]), final)

    want_wire = fix["expect"]["wire"]
    ok = wire == want_wire
    results.append((ok, "wire == " + json.dumps(want_wire)
                        + ("" if ok else f" (diff: {dict_diff(wire, want_wire)})")))

    for chk in fix["expect"]["checks"]:
        results.append(run_check(chk, fix, posted))

    status = "PASS" if all(ok for ok, _ in results) else "FAIL"
    if verbose:
        for a in posted:
            print(f"      posted: {json.dumps(a, ensure_ascii=False)[:140]}")
    return {"name": fix["name"], "status": status, "results": results,
            "notes": fix.get("notes", []), "ladder": ladder, "wire": wire}


# ---------------------------------------------------------------------------
# --live STUB. Written + arg-gated; deliberately NEVER executed by CI or by
# the authoring run. It reuses the same fixtures but lets the REAL agents
# moderate them, then scores only the agent-independent safety expectations
# plus two heuristics. Treat results as a smoke signal, not a benchmark.
# ---------------------------------------------------------------------------

def _decl_exists_in_mathlib(decl: str) -> bool:
    """Heuristic decl-sanity for live mode: the cited decl name occurs
    somewhere under Mathlib/ in the local checkout (existence, not semantics)."""
    mathlib = Path(str(ba.MATHLIB)) if ba is not None else None
    if not mathlib or not (mathlib / "Mathlib").exists():
        return True  # can't check — don't fail the heuristic on environment
    short = decl.rsplit(".", 1)[-1]
    proc = subprocess.run(
        ["grep", "-rl", "--include=*.lean", "-F", short, str(mathlib / "Mathlib")],
        capture_output=True, text=True, timeout=120)
    return proc.returncode == 0


def eval_live_scenario(fix: dict, verbose: bool) -> dict:
    """LIVE STUB: real agents, same safety scoring + heuristics."""
    if fix.get("pipeline") == "create":
        # Creation live-eval would need a real Wikipedia article; the planted
        # envelope path is already fully deterministic — nothing to run live.
        return {"name": fix["name"], "status": "SKIP", "results": [],
                "notes": ["create scenario has no live variant"]}
    if ba is None:
        return {"name": fix["name"], "status": "SKIP", "results": [],
                "notes": ["batch_annotate unavailable"]}
    rec, final = run_moderate_pipeline(fix, live=True)
    if rec.get("error"):
        return {"name": fix["name"], "status": "FAIL",
                "results": [(False, f"pipeline error: {rec['error']}")], "notes": []}
    posted, wire = m.finalize_for_post(copy.deepcopy(fix["existing"]), final)
    results: list[tuple[bool, str]] = []
    # Agent-independent safety expectations (subset of the offline checks).
    for chk in fix["expect"]["checks"]:
        if chk["check"] in ("humans_intact", "human_verbatim", "label_status",
                            "all_ids_unique_hex", "id_absent"):
            results.append(run_check(chk, fix, posted))
    # Heuristic 1: coverage must not shrink.
    ok = len(posted) >= len(fix["existing"])
    results.append((ok, f"coverage heuristic: {len(posted)} >= "
                        f"{len(fix['existing'])} existing"))
    # Heuristic 2: every cited decl exists in the local Mathlib checkout.
    bad = [a["mathlib"]["decl"] for a in posted
           if isinstance(a.get("mathlib"), dict) and a["mathlib"].get("decl")
           and not _decl_exists_in_mathlib(a["mathlib"]["decl"])]
    results.append((not bad, f"decl-sanity heuristic (hallucinated: {bad})"))
    status = "PASS" if all(ok for ok, _ in results) else "FAIL"
    return {"name": fix["name"], "status": status, "results": results,
            "notes": ["LIVE run — tokens were spent"], "wire": wire,
            "tokens": rec.get("tokens")}


# ---------------------------------------------------------------------------
# Scorecard + main
# ---------------------------------------------------------------------------

def fmt_ladder(d: dict | None) -> str:
    if not d:
        return "-"
    return f"{d.get('restored', 0)}/{d.get('reinserted', 0)}/{d.get('downgrades_blocked', 0)}"


def fmt_wire(d: dict | None) -> str:
    if not d:
        return "-"
    keys = ("ids_echoed", "ids_inherited", "ids_fresh", "human_restored_wire",
            "human_reinserted_wire", "provenance_downgraded",
            "veto_adjacent_dropped")
    return "/".join(str(d.get(k, 0)) for k in keys)


def print_scorecard(rows: list[dict]) -> None:
    print()
    hdr = (f"{'scenario':<22} {'checks':>7}  {'ladder r/i/d':<13} "
           f"{'wire e/i/f/hr/hri/pd/vad':<25} result")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        n_ok = sum(1 for ok, _ in r["results"] if ok)
        n = len(r["results"])
        checks = f"{n_ok}/{n}" if n else "-"
        print(f"{r['name']:<22} {checks:>7}  {fmt_ladder(r.get('ladder')):<13} "
              f"{fmt_wire(r.get('wire')):<25} {r['status']}")
    print("-" * len(hdr))
    n_pass = sum(1 for r in rows if r["status"] == "PASS")
    n_fail = sum(1 for r in rows if r["status"] == "FAIL")
    n_skip = sum(1 for r in rows if r["status"] == "SKIP")
    print(f"{n_pass} passed, {n_fail} failed, {n_skip} skipped")


def load_fixtures(only: str | None) -> list[dict]:
    paths = sorted(FIXTURES_DIR.glob("*.json"))
    fixtures = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    if only:
        fixtures = [f for f in fixtures if f["name"] == only]
        if not fixtures:
            sys.exit(f"no fixture named {only!r} in {FIXTURES_DIR}")
    return fixtures


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic moderation-behavior evals (CI gate) "
                    "+ a gated live-agent stub.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true",
                      help="deterministic post-pipeline over planted agent "
                           "outputs — zero tokens, zero network (the CI gate)")
    mode.add_argument("--live", action="store_true",
                      help="STUB: run the REAL agents on the fixtures; "
                           "requires --i-understand-this-costs-tokens")
    ap.add_argument("--i-understand-this-costs-tokens", action="store_true",
                    help="confirmation gate for --live")
    ap.add_argument("--only", default=None, metavar="NAME",
                    help="run a single named scenario")
    ap.add_argument("--require-all", action="store_true",
                    help="treat SKIPped scenarios as failures (CI under the venv)")
    ap.add_argument("--verbose", action="store_true",
                    help="dump each scenario's wire payload + per-check lines")
    args = ap.parse_args()

    if args.live and not args.i_understand_this_costs_tokens:
        ap.exit(2, "ERROR: --live runs the REAL agents and costs tokens/Max-plan "
                   "window. Re-run with --i-understand-this-costs-tokens if you "
                   "mean it (CI must use --offline).\n")

    fixtures = load_fixtures(args.only)
    runner = eval_live_scenario if args.live else eval_offline_scenario
    label = "LIVE (real agents)" if args.live else "offline (deterministic)"
    print(f"eval_moderation: {len(fixtures)} scenario(s), mode={label}")

    rows = []
    for fix in fixtures:
        r = runner(fix, args.verbose)
        rows.append(r)
        marker = {"PASS": "ok", "FAIL": "FAIL", "SKIP": "skip"}[r["status"]]
        print(f"  [{marker:>4}] {r['name']}")
        for ok, msg in r["results"]:
            if args.verbose or not ok:
                print(f"      {'PASS' if ok else 'FAIL'}  {msg}")
        for note in r.get("notes", []):
            print(f"      note: {note}")

    print_scorecard(rows)
    failed = any(r["status"] == "FAIL" for r in rows)
    skipped = any(r["status"] == "SKIP" for r in rows)
    if failed:
        return 1
    if skipped and args.require_all:
        print("FAIL: scenarios were skipped and --require-all is set")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
