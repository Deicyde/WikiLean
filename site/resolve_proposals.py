#!/usr/bin/env python3
"""Deterministic proposal resolver — the AI decide path's nightly engine.

Human-at-boundaries (ROADMAP binding decision, ratified 2026-08-06): the AI
decides intra-WikiLean proposals; humans gate only cross-site pushes. This
script is that decider for the DETERMINISTIC subset — NO LLM anywhere. It
reads the pending queue from GET /api/proposals (the bearer-only machine twin
of /proposals — same row-builder, same applyProposalFields delta) and applies
three rules; anything they don't cover stays pending for a human or a smarter
agent:

  R1  noChange (the server-computed delta is EMPTY — a confirmation
      masquerading as a change)                  -> reject, reason not_better
  R2  decl rename: the proposal's mathlib.decl EXISTS in the union oracle
      AND the current annotation's mathlib.decl is GONE from it
                                                 -> approve
  R3  proof_wanted downgrade: the cited decl is a proof_wanted stub and the
      proposal sets status 'partial' ('formalized' alone is an overclaim —
      owner-ratified policy, commit 9d2d042f)    -> approve
  R4  anything else                              -> leave pending + print why

Oracle discipline (project memory, non-negotiable): the UNION oracle
(doc-gen4 cache + mathlib4 source parse via manage/decl_existence_sweep.py's
loaders — the cache alone MISSES real decls), and decl names are matched
EXACTLY — never by bare suffix.

Dry-run by default: prints every verdict, writes nothing. --submit sends the
decisions through POST /api/article/:slug {action: approve_proposal |
reject_proposal} with the PIPELINE_TOKEN bearer (the Worker stamps
provenance/attribution server-side — an AI approve over a human annotation
becomes 'ai-moderated', never stays 'human').

Nightly: site/ops/nightly-moderate.sh runs `--submit` after the review step,
gated by WIKILEAN_AUTO_DECIDE (site/ops/nightly.env, default 1). A failure
here must never kill the rest of the night (the wrapper fail-softs).

Run: python3 site/resolve_proposals.py [--submit] [--api-base URL]
     [--mathlib DIR] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The macOS framework Python ships no CA bundle — use certifi when present
# (same workaround as manage/halo.py + brain/ingest/lean_repo.py).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # the venv python ($PY in the nightly) has its own bundle
    _SSL_CTX = ssl.create_default_context()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "site"))
sys.path.insert(0, str(REPO / "manage"))

from moderate import DEFAULT_API_BASE, resolve_token  # noqa: E402
from decl_existence_sweep import (  # noqa: E402
    DEFAULT_MATHLIB,
    load_cache_oracle,
    parse_source_names,
)

USER_AGENT = "wikilean-resolve-proposals/1 (deterministic; no LLM)"


# ---------------------------------------------------------------------------
# Pure verdict logic (unit-tested by site/test_moderate.py without network)
# ---------------------------------------------------------------------------
def decl_of(mathlib_field: object) -> str | None:
    """The decl name inside an annotation/proposal `mathlib` field.

    Shape per the editor + pipeline: {"decl": str, "module": str, ...}; legacy
    rows may carry a bare string. Anything else -> None (never guess)."""
    if isinstance(mathlib_field, str):
        return mathlib_field.strip() or None
    if isinstance(mathlib_field, dict):
        d = mathlib_field.get("decl")
        return d.strip() if isinstance(d, str) and d.strip() else None
    return None


def verdict(row: dict, oracle: set[str], proof_wanted: set[str]) -> tuple[str, str]:
    """(action, why) for one /api/proposals row.

    action ∈ {'reject', 'approve', 'pending'}; `why` names the rule for the
    log + the research exports. EXACT name matching only — a suffix match is
    the bare-suffix-guess trap and is structurally impossible here (set
    membership on fully-qualified names).
    """
    # Target gone/unparseable: approve would 409-drop it as stale server-side.
    # Not ours to decide — the bot-save sweep self-heals these.
    if row.get("changed") is None:
        return ("pending", "target gone — server sweep will mark it stale")

    # R1 — empty delta: approving is a pure no-op, so the proposal is a
    # confirmation, and the current annotation is by definition "at least as
    # good" (the not_better enum's exact meaning). (Known narrow race: the
    # verdict reads GET-time state, and reject permanently suppresses this
    # exact delta via fieldsSig — acceptable for a seconds-wide window on a
    # queue this small; revisit if proposal volume grows.)
    if row.get("noChange"):
        return ("reject", "R1 no-delta confirmation -> not_better")

    fields = row.get("fields") or {}
    current = row.get("current") or {}
    new_decl = decl_of(fields.get("mathlib"))
    old_decl = decl_of(current.get("mathlib"))
    new_status = fields.get("status")
    # RIDER GUARD (adversarial-review major): approve applies the WHOLE
    # whitelisted delta, so a rule may only approve when the server-computed
    # changed-field set is covered by what the rule actually verified —
    # unexamined note/label/kind rewrites (or a malformed mathlib wipe) must
    # never ride an approve into a human annotation.
    changed_fields = {c.get("field") for c in (row.get("changed") or [])}
    # Effective post-approve status: a proposal that omits `status` KEEPS the
    # current one (adversarial-review major: a pure rename onto a stub must
    # not leave a standing 'formalized' overclaim in place).
    eff_status = fields.get("status", current.get("status"))

    # The rename leg, verified once and shared by R2/R3 (exact names — a bare
    # suffix is structurally impossible: set membership on FQ names).
    rename_ok = False
    if new_decl and old_decl and new_decl != old_decl:
        rename_ok = new_decl in oracle and old_decl not in oracle

    # R3 — proof_wanted downgrade (checked BEFORE R2: a rename onto a
    # proof_wanted stub must still land as 'partial', not silently approve a
    # 'formalized' overclaim through the rename rule).
    cited = new_decl or old_decl
    if cited and cited in proof_wanted:
        if new_status == "partial":
            if changed_fields <= {"status"} or (changed_fields <= {"status", "mathlib"} and rename_ok):
                return ("approve", f"R3 {cited} is a proof_wanted stub; 'partial' is the ratified status")
            return ("pending", f"R3 status ok but unverified rider fields "
                               f"{sorted(changed_fields - {'status', 'mathlib'}) or sorted(changed_fields)} — human call")
        if "status" in fields:
            return ("pending", f"proof_wanted stub {cited} but proposed status "
                               f"{new_status!r} != 'partial' — human call")

    # R2 — verified decl rename: the new name exists, the old one is gone.
    if new_decl and old_decl and new_decl != old_decl:
        if rename_ok:
            # A rename may not smuggle an overclaim — judged on the EFFECTIVE
            # status (proposed, else the current one that stays standing).
            if eff_status == "formalized" and new_decl in proof_wanted:
                return ("pending", f"R2 rename ok but {new_decl} is proof_wanted and the "
                                   f"effective status is 'formalized' — overclaim, human call")
            if not changed_fields <= {"mathlib"}:
                return ("pending", f"R2 rename ok but unverified rider fields "
                                   f"{sorted(changed_fields - {'mathlib'})} — human call")
            return ("approve", f"R2 rename {old_decl} -> {new_decl} "
                               f"(new exists, old gone from the union oracle)")
        return ("pending", f"R2 not clean: new {new_decl!r} in oracle={new_decl in oracle}, "
                           f"old {old_decl!r} gone={old_decl not in oracle}")

    return ("pending", "no deterministic rule applies")


# ---------------------------------------------------------------------------
# API plumbing
# ---------------------------------------------------------------------------
def api(base: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:  # noqa: BLE001
            return e.code, {"error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--submit", action="store_true",
                    help="actually decide (default: dry-run, print only)")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--mathlib", type=Path, default=DEFAULT_MATHLIB)
    ap.add_argument("--limit", type=int, default=100,
                    help="max decisions to submit in one run (safety valve)")
    args = ap.parse_args()

    token = resolve_token()
    if not token:
        print("no PIPELINE_TOKEN / WIKILEAN_API_TOKEN — cannot read the queue",
              file=sys.stderr)
        return 1

    status, body = api(args.api_base, "/api/proposals", token)
    if status != 200 or not body.get("ok"):
        print(f"GET /api/proposals -> {status}: {body}", file=sys.stderr)
        return 1
    rows = body.get("rows") or []
    print(f"pending proposals: {body.get('total')} (rows fetched: {len(rows)})")
    if not rows:
        return 0

    # Union oracle — assembled once per run; loud when a leg is missing.
    cache = load_cache_oracle()
    src_names, proof_wanted = parse_source_names(args.mathlib)
    oracle = cache | src_names
    print(f"oracle: {len(cache)} cache + {len(src_names)} source = "
          f"{len(oracle)} union; {len(proof_wanted)} proof_wanted stubs")
    if not src_names:
        # Cache-only oracle can't prove old-decl-gone (renames look like
        # hallucinations) — R2 must not fire. Downgrade it to pending.
        print("source oracle EMPTY — R2 disabled this run (cannot prove "
              "'old is gone' from the cache alone)", file=sys.stderr)

    decided = 0
    tally = {"approve": 0, "reject": 0, "pending": 0, "error": 0}
    for row in rows:
        action, why = verdict(row, oracle, proof_wanted)
        if action == "approve" and why.startswith("R2") and not src_names:
            action, why = "pending", "R2 suppressed: source oracle empty"
        pid, slug = row.get("proposalId"), row.get("slug")
        print(f"  [{action:7s}] {slug} {pid} — {why}")
        if action == "pending" or not args.submit:
            tally[action] += 1   # dry-run tallies the verdicts themselves
            continue
        if decided >= args.limit:
            print(f"  limit {args.limit} reached — remaining stay pending")
            break
        decided += 1  # the valve counts ATTEMPTS, not just 200s — a night of
        #               failures must not turn into 200 uncapped POSTs
        if action == "reject":
            st, resp = api(args.api_base, f"/api/article/{slug}", token,
                           {"action": "reject_proposal", "proposal_id": pid,
                            "reject_reason": "not_better"})
        else:
            st, resp = api(args.api_base, f"/api/article/{slug}", token,
                           {"action": "approve_proposal", "proposal_id": pid,
                            "base_version": row.get("version")})
        if st == 200:
            tally[action] += 1
        elif st == 409:
            why409 = resp.get("error")
            if why409 == "stale":
                # CAS lost (another write bumped the version since our GET) —
                # NOT resolved; the next nightly re-fetches and retries.
                print("      409 stale CAS — retried next run")
            else:
                # dead target: the server marked the proposal stale itself
                print(f"      409 ({why409}) — server resolved it")
            tally["pending"] += 1
        else:
            tally["error"] += 1
            print(f"      FAILED {st}: {resp}", file=sys.stderr)

    mode = "submitted" if args.submit else "DRY-RUN (nothing written)"
    print(f"{mode}: {tally}")
    return 0 if tally["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
