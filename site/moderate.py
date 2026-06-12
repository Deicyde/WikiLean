#!/usr/bin/env python3
"""Unified moderation runner for WikiLean (D1-direct).

The three routine operations from the roadmap, as subcommands:

    new        annotate articles not in D1 yet and CREATE them via the
               bot-only PUT /api/article/:slug (contract D-C1). Candidates
               come from --from-file (discover_articles.py JSONL) or, by
               default, the catalog 404-probe fallback.
    review     re-review existing articles (work list from GET /api/work);
               agents run against the article's PINNED Wikipedia revision
               (revid-suffixed cache), never a stale/live page (F1).
    wp-update  stage-0 deterministic re-pin, driven through
               update_from_upstream.process_slug (F3; the script stays
               usable standalone).
    all        wp-update, then review, then new (F3). ORDER RATIONALE:
               stage-0 clears ~80% of drift for ZERO tokens and resets
               wp_drifted, so running it first — and only then fetching the
               review job list fresh — keeps review tokens off articles whose
               only problem was upstream drift (/api/work sorts drifted
               articles high; re-pinned ones fall back to their age tier).

Reads via GET /api/article/:slug.json (bearer-authenticated: the Worker
filters tombstones from anonymous responses, and the runner must see them —
F15), writes via bearer-authenticated POST /api/article/:slug with
base_version (409 → one zero-token rebase: re-GET, re-finalize, re-POST — F9).
Reuses the agent machinery from batch_annotate.py (annotate_one /
_preserve_human / the PRIORITY ladder); this module adds work selection, the
annotation-ID discipline post-pass (contract ID1), wire-level human
preservation (the client-side twin of the server's 422 check, plus the
veto-adjacency drop for near-miss tombstone resurrections — F6), and run
metadata (contract ID3).

Auth token: WIKILEAN_API_TOKEN env var, else PIPELINE_TOKEN from wiki/.dev.vars.
Mathlib checkout: WIKILEAN_MATHLIB env var (read by batch_annotate.MATHLIB).

Run with the venv that has claude-agent-sdk:
    catalog/.venv/bin/python site/moderate.py review --limit 3 --dry-run
    catalog/.venv/bin/python site/moderate.py review --limit 3
    catalog/.venv/bin/python site/moderate.py new --limit 5 --auth api-key
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_DATA = REPO / "catalog" / "data"
DEFAULT_API_BASE = "https://wikilean.jackmccarthy.org"
USER_AGENT = ("WikiLean-moderate/0.1 (https://github.com/Deicyde/WikiLean; "
              "jack.mccarthy.1@stonybrook.edu)")
MODERATE_LOG = HERE / "cache" / ".moderate_run.log"
# Consecutive window-exhaustion errors before aborting (batch_annotate.run uses
# 15 for 700-article sweeps; moderate batches are small, so trip earlier).
ABORT_AFTER = 5
# new-mode candidate probing is one GET per catalog title until --limit new
# slugs are found; cap total probes so a mostly-seeded catalog doesn't turn
# into a 1,300-request scan. discover_articles.py (Wave C3+) will replace this
# with a proper WikiProject-list-vs-D1 diff feed.
MAX_NEW_PROBES = 400

# batch_annotate is imported LAZILY (see _import_ba): it pops ANTHROPIC_API_KEY
# at import time unless WIKILEAN_KEEP_API_KEY=1, so --auth must be parsed first.
ba = None


# ---------------------------------------------------------------------------
# Pure helpers (stdlib-only — unit-tested by test_moderate.py without the SDK)
# ---------------------------------------------------------------------------

def _anchor_sig(a: dict) -> str:
    """Stable signature of an annotation's anchor. MUST stay in lockstep with
    batch_annotate._anchor_sig (copied here so this module imports without the
    SDK; test_moderate.py asserts the two stay identical).

    LOCKSTEP CONTRACT (F12 — identical in batch_annotate._anchor_sig and
    wiki/src/validation.ts anchorSig): a plain-dict `anchor` is used as before;
    a non-dict `anchor` (string/list/number — must not crash) falls through to
    `anchors[0]` when `anchors` is a non-empty list whose first element is a
    plain dict; otherwise the all-null signature."""
    anc = a.get("anchor")
    if not isinstance(anc, dict):
        anchors = a.get("anchors")
        if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict):
            anc = anchors[0]
        else:
            anc = {}
    return json.dumps([anc.get("type"), anc.get("section"), anc.get("snippet"),
                       anc.get("value"), anc.get("from")], sort_keys=True)


def fresh_id(used: set[str]) -> str:
    """Contract ID1: 12-char lowercase hex, crypto-random."""
    nid = secrets.token_hex(6)
    while nid in used:
        nid = secrets.token_hex(6)
    return nid


def _snippet_tokens(s) -> set[str]:
    """Lowercased word tokens of a snippet (helper for the F6 veto check)."""
    return set(re.findall(r"\w+", s.lower())) if isinstance(s, str) else set()


def _veto_adjacent(a: dict, tombstones: list[dict]) -> bool:
    """F6: True when `a` (a produced annotation with NO stored counterpart by
    id or anchor sig) lands in a stored tombstone's section AND its snippet
    contains >50% of the tombstone snippet's word tokens — a near-miss
    resurrection of a human veto. Exact-sig resurrections are already replaced
    by the stored tombstone; this closes the shifted-anchor bypass."""
    anc = a.get("anchor") if isinstance(a.get("anchor"), dict) else {}
    toks = _snippet_tokens(anc.get("snippet"))
    if not toks:
        return False
    for t in tombstones:
        tanc = t.get("anchor") if isinstance(t.get("anchor"), dict) else {}
        ttoks = _snippet_tokens(tanc.get("snippet"))
        if not ttoks or anc.get("section") != tanc.get("section"):
            continue
        if len(toks & ttoks) / len(ttoks) > 0.5:
            return True
    return False


def finalize_for_post(existing: list[dict], produced: list[dict],
                      ) -> tuple[list[dict], dict]:
    """Deterministic post-pass between the agent pipeline and the bot POST.

    ID discipline (contract ID1):
      - an existing id echoed verbatim survives;
      - an output annotation with an unknown or missing id inherits the id of
        the existing annotation with the same anchor signature, if it has one;
      - otherwise it is NEW and gets a fresh 12-hex id.

    Wire-level human preservation (client twin of the server's 422 check,
    which deep-equals stored provenance='human' annotations — tombstones
    included — against the posted array):
      - every stored human annotation is posted BYTE-IDENTICAL (this strips
        agent-added fields like moderation_flag and _preserve_human's
        moderation_note, and never adds an id the stored copy lacks — id
        backfill for human annotations is SQL-only, per contract ID2);
      - a stored human annotation absent from the output is re-appended;
      - a non-matching output annotation claiming provenance 'human' is
        downgraded to 'ai-moderated' (bot writes must not mint human
        provenance — anti-laundering, mirrors the server's session-side rule);
      - a PRODUCED new annotation (no stored counterpart by id or anchor sig)
        that near-misses a stored tombstone — same anchor section AND >50%
        token containment of the tombstone's snippet — is DROPPED and counted
        as veto_adjacent_dropped (F6: shifted-anchor resurrections of human
        vetoes don't reach the wire).
    """
    humans = [a for a in existing if a.get("provenance") == "human"]
    tombs = [a for a in existing if a.get("status") == "rejected"]
    h_by_id = {a["id"]: i for i, a in enumerate(humans)
               if isinstance(a.get("id"), str) and a["id"]}
    h_by_sig: dict[str, int] = {}
    for i, a in enumerate(humans):
        h_by_sig.setdefault(_anchor_sig(a), i)
    ex_by_id = {a["id"]: a for a in existing
                if isinstance(a.get("id"), str) and a["id"]}
    ex_by_sig: dict[str, dict] = {}
    for a in existing:
        ex_by_sig.setdefault(_anchor_sig(a), a)

    out: list[dict] = []
    used_ids: set[str] = set()
    consumed: set[int] = set()
    stats = {"ids_echoed": 0, "ids_inherited": 0, "ids_fresh": 0,
             "human_restored_wire": 0, "human_reinserted_wire": 0,
             "provenance_downgraded": 0, "veto_adjacent_dropped": 0}

    for a in produced:
        aid = a.get("id") if isinstance(a.get("id"), str) and a.get("id") else None
        # 1. stored-human counterpart (by id, else anchor sig) → post the
        #    stored original verbatim; any deviation would 422 server-side.
        hi = h_by_id.get(aid)
        if hi is None or hi in consumed:
            hi = h_by_sig.get(_anchor_sig(a))
        if hi is not None and hi not in consumed:
            h = humans[hi]
            consumed.add(hi)
            if isinstance(h.get("id"), str) and h["id"]:
                used_ids.add(h["id"])
            if a != h:
                stats["human_restored_wire"] += 1
            out.append(h)
            continue
        # 2. veto adjacency (F6): a NEW annotation (no stored counterpart by
        #    id or sig) near-missing a stored tombstone's anchor is dropped.
        if (tombs and (aid is None or aid not in ex_by_id)
                and _anchor_sig(a) not in ex_by_sig
                and _veto_adjacent(a, tombs)):
            stats["veto_adjacent_dropped"] += 1
            continue
        # 3. anti-laundering: 'human' provenance without a stored human twin.
        if a.get("provenance") == "human":
            a = {**a, "provenance": "ai-moderated"}
            stats["provenance_downgraded"] += 1
        # 4. id discipline.
        if aid is not None and aid in ex_by_id and aid not in used_ids:
            used_ids.add(aid)
            stats["ids_echoed"] += 1
            out.append(a)
            continue
        ex = ex_by_sig.get(_anchor_sig(a))
        exid = ex.get("id") if ex else None
        if isinstance(exid, str) and exid and exid not in used_ids:
            used_ids.add(exid)
            stats["ids_inherited"] += 1
            out.append({**a, "id": exid})
        else:
            nid = fresh_id(used_ids)
            used_ids.add(nid)
            stats["ids_fresh"] += 1
            out.append({**a, "id": nid})

    # 5. dropped humans (tombstones included — human vetoes) → re-insert verbatim.
    for i, h in enumerate(humans):
        if i not in consumed:
            out.append(h)
            stats["human_reinserted_wire"] += 1
    return out, stats


def resolve_token() -> str | None:
    """WIKILEAN_API_TOKEN env var, else PIPELINE_TOKEN from wiki/.dev.vars."""
    tok = os.environ.get("WIKILEAN_API_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    dev = REPO / "wiki" / ".dev.vars"
    if dev.exists():
        for line in dev.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PIPELINE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def build_meta(ctx, rec: dict, wire_stats: dict) -> dict:
    """Per-write revisions.meta payload (contract ID3)."""
    a1 = rec.get("agent1_meta") or {}
    a2 = rec.get("agent2_meta") or {}
    duration_ms = ((a1.get("duration_ms") or 0) + (a2.get("duration_ms") or 0)
                   or int((rec.get("elapsed_s") or 0) * 1000))
    ladder = {"restored": 0, "reinserted": 0, "downgrades_blocked": 0,
              "moderation_flags": []}  # F14: harvested agent dissent rides meta
    ladder.update(rec.get("ladder") or {})
    return {
        "run_id": ctx.run_id,
        "mode": ctx.mode,
        "model": ctx.model,
        "prompt_sha": ctx.prompt_sha,
        "tokens": rec.get("tokens") or 0,
        "cost_usd_equiv": rec.get("cost_usd_equiv"),
        "duration_ms": duration_ms,
        "mathlib_sha": ctx.mathlib_sha,
        "auth_mode": ctx.auth,
        "ladder": ladder,
        "ids": wire_stats,
    }


def load_candidate_file(path: Path) -> list[dict]:
    """Parse discover_articles.py output (JSONL of {"title","slug","source"}).
    Malformed lines and records missing title/slug are skipped with a count."""
    cands: list[dict] = []
    skipped = 0
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        title = rec.get("title") if isinstance(rec, dict) else None
        slug = rec.get("slug") if isinstance(rec, dict) else None
        if (not isinstance(title, str) or not title
                or not isinstance(slug, str) or not slug or slug in seen):
            skipped += 1
            continue
        seen.add(slug)
        cands.append({"title": title, "slug": slug,
                      "source": rec.get("source") or "from-file"})
    if skipped:
        print(f"  note: skipped {skipped} malformed/duplicate line(s) in {path}")
    return cands


def sidecar_revid(slug: str, cache_dir: Path | None = None) -> int | None:
    """Pinned Wikipedia revid from the fetch sidecar cache/<slug>.meta.json
    (written by batch_annotate.fetch_html). None when absent/unreadable."""
    p = (cache_dir or HERE / "cache") / f"{slug}.meta.json"
    try:
        revid = json.loads(p.read_text(encoding="utf-8")).get("revid")
    except (OSError, json.JSONDecodeError):
        return None
    return revid if isinstance(revid, int) and revid > 0 else None


def load_qid_map(data_dir: Path | None = None) -> dict[str, str]:
    """title → wikidata_qid from the tagged catalog snapshots (the discovery
    feed carries no QIDs, so absent titles simply get no wikidata_qid)."""
    qids: dict[str, str] = {}
    for name in ("pilot_tagged.jsonl", "tier2_tagged.jsonl"):
        p = (data_dir or CATALOG_DATA) / name
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            title, qid = rec.get("title"), rec.get("wikidata_qid")
            if isinstance(title, str) and isinstance(qid, str) and qid:
                qids.setdefault(title, qid)
    return qids


def build_create_body(envelope: dict, *, revid: int | None = None,
                      wikidata_qid: str | None = None, run_id: str = "",
                      ) -> tuple[dict, dict]:
    """PUT /api/article/:slug body (contract D-C1) from a pipeline envelope
    (site/annotations/<slug>.json shape). Returns (body, wire_stats); the
    caller attaches body["meta"] = build_meta(...) built from wire_stats.

    Annotations go through finalize_for_post([], …): every one gets a fresh
    12-hex id (the server heals ids anyway, but minting here keeps the disk
    artifact and D1 in agreement) and any provenance 'human' claim is
    downgraded — a create has no stored humans to launder from."""
    annotations, wire = finalize_for_post([], envelope.get("annotations") or [])
    body: dict = {
        "wikipedia_title": envelope["wikipedia_title"],
        "annotations": annotations,
        "comment": f"ai-create:{run_id}",
    }
    dt = envelope.get("display_title")
    if isinstance(dt, str) and dt:
        body["display_title"] = dt
    if isinstance(wikidata_qid, str) and wikidata_qid:
        body["wikidata_qid"] = wikidata_qid
    if isinstance(revid, int) and not isinstance(revid, bool) and revid > 0:
        body["revid"] = revid
    return body, wire


# ---------------------------------------------------------------------------
# Lazy imports + run fingerprints
# ---------------------------------------------------------------------------

def _import_ba(auth: str):
    """Import batch_annotate AFTER deciding the auth mode — its module-level
    ANTHROPIC_API_KEY pop respects WIKILEAN_KEEP_API_KEY (set here)."""
    global ba
    if ba is None:
        if auth == "api-key":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("WARNING: --auth api-key but ANTHROPIC_API_KEY is unset",
                      file=sys.stderr)
            os.environ["WIKILEAN_KEEP_API_KEY"] = "1"
        else:
            os.environ.pop("WIKILEAN_KEEP_API_KEY", None)
        sys.path.insert(0, str(HERE))
        import batch_annotate
        ba = batch_annotate
    return ba


def _try_import_ba(auth: str):
    """Best-effort import for --dry-run (works without the SDK installed)."""
    try:
        return _import_ba(auth)
    except ImportError as e:
        print(f"(batch_annotate unavailable: {e} — dry-run with placeholders)")
        return None


def get_mathlib_sha() -> str | None:
    mlib = (str(ba.MATHLIB) if ba is not None
            else os.environ.get("WIKILEAN_MATHLIB", "/Users/jack/Desktop/LEAN/mathlib4"))
    try:
        proc = subprocess.run(["git", "-C", mlib, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() or None if proc.returncode == 0 else None
    except OSError:
        return None


def get_prompt_sha(ba_mod, mode: str) -> str:
    """First 12 hex of sha256 over the system prompts the mode uses (Agent 1's
    mode-specific prompt + the shared Agent 2 prompt)."""
    if ba_mod is None:
        return "unavailable"
    a1 = ba_mod.MODERATE_AGENT1_SYSTEM if mode == "review" else ba_mod.AGENT1_SYSTEM
    return hashlib.sha256((a1 + "\n" + ba_mod.AGENT2_SYSTEM)
                          .encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# HTTP (lazy requests import keeps this module importable stdlib-only)
# ---------------------------------------------------------------------------

def _http_get(url: str, token: str | None = None):
    import requests
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(url, headers=headers, timeout=60)


def fetch_work(api_base: str, token: str, mode: str, limit: int) -> list[dict]:
    """GET /api/work (bearer-only). Server caps limit at 100."""
    if limit > 100:
        print("note: /api/work caps limit at 100")
        limit = 100
    r = _http_get(f"{api_base}/api/work?mode={mode}&limit={limit}", token)
    if r.status_code != 200:
        sys.exit(f"GET /api/work?mode={mode} failed: {r.status_code} {r.text[:200]}")
    return r.json().get("jobs", [])


async def get_article(api_base: str, slug: str,
                      token: str | None = None) -> tuple[dict | None, int]:
    # F15: runner GETs send the bearer header — the Worker filters tombstones
    # from anonymous responses, and the runner must keep seeing them.
    url = f"{api_base}/api/article/{urllib.parse.quote(slug)}.json"
    r = await asyncio.to_thread(_http_get, url, token)
    if r.status_code != 200:
        return None, r.status_code
    return r.json(), 200


async def _write_article(ctx, slug: str, body: dict, method: str,
                         ) -> tuple[int, dict]:
    """Bearer write with the contract's error handling:
      - 429 → sleep 60s, retry up to 3x (EDIT_LIMITER is 30 writes/min);
      - 5xx → retry twice with 5s/15s backoff (F9; the CAS guard makes a
        committed-then-500 retry safe — it 409s into the rebase path);
      - POST 409 → ONE zero-agent-token rebase (F9): re-GET the article,
        re-run finalize_for_post against the fresh state, re-POST. A second
        409 (or a PUT 409 = 'exists') is returned to the caller."""
    import requests
    url = f"{ctx.api_base}/api/article/{urllib.parse.quote(slug)}"
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {ctx.token}"}
    send = requests.put if method == "put" else requests.post
    r = None
    retries_429 = 0
    backoffs_5xx = [5, 15]
    rebased = False
    while True:
        r = await asyncio.to_thread(send, url, json=body, headers=headers,
                                    timeout=120)
        if r.status_code == 429 and retries_429 < 3:
            retries_429 += 1
            print(f"  429 on {slug} — sleeping 60s (retry {retries_429}/3)", flush=True)
            await asyncio.sleep(60)
            continue
        if 500 <= r.status_code < 600 and backoffs_5xx:
            wait = backoffs_5xx.pop(0)
            print(f"  {r.status_code} on {slug} — retrying in {wait}s (F9; "
                  f"CAS makes this safe)", flush=True)
            await asyncio.sleep(wait)
            continue
        if (r.status_code == 409 and method == "post" and not rebased
                and "base_version" in body):
            rebased = True
            art, status = await get_article(ctx.api_base, slug, ctx.token)
            if art is not None:
                posted, wire = finalize_for_post(
                    art.get("annotations") or [], body.get("annotations") or [])
                body = {**body, "annotations": posted,
                        "base_version": art["version"]}
                if isinstance(body.get("meta"), dict):
                    body["meta"] = {**body["meta"], "ids": wire}
                print(f"  409 on {slug} — rebased onto version {art['version']} "
                      f"and re-POSTing once (zero agent tokens)", flush=True)
                continue
            print(f"  409 on {slug} — rebase re-GET failed ({status}); "
                  f"returning the 409", flush=True)
        break
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload


async def post_article(ctx, slug: str, body: dict) -> tuple[int, dict]:
    """POST /api/article/:slug — save over an existing article."""
    return await _write_article(ctx, slug, body, "post")


async def put_article(ctx, slug: str, body: dict) -> tuple[int, dict]:
    """PUT /api/article/:slug — bot-only article create (contract D-C1)."""
    return await _write_article(ctx, slug, body, "put")


# ---------------------------------------------------------------------------
# Per-job flows
# ---------------------------------------------------------------------------

async def process_review(job: dict, ctx, sem: asyncio.Semaphore) -> dict:
    """review: GET live annotations → agents (existing_override) → ID post-pass
    → bearer POST with base_version. The agents review the article's PINNED
    Wikipedia revision (F1): the GET's revid is threaded into the fetch
    (revid-suffixed cache, oldid= on miss) and section extraction runs on that
    HTML — but the POST still carries NO revid, so the pin is unchanged.
    Dry-run: pass existing through unchanged, print what WOULD be posted,
    zero POSTs and zero agent calls."""
    slug = job["slug"]
    rec: dict = {"slug": slug, "op": ctx.mode, "reason": job.get("reason")}
    art, status = await get_article(ctx.api_base, slug, ctx.token)
    if art is None:
        rec["error"] = f"get_failed_{status}"
        return rec
    existing = art.get("annotations") or []
    base_version = art["version"]
    title = art.get("wikipedia_title") or slug.replace("_", " ")
    rec["base_version"] = base_version
    # F1: the pinned revid drives the fetch; a missing/invalid pin (shouldn't
    # happen — all production revids verified non-null) falls back to legacy.
    pinned = art.get("revid")
    target_revid = (pinned if isinstance(pinned, int)
                    and not isinstance(pinned, bool) and pinned > 0 else None)
    if target_revid is None:
        print(f"  ! {slug}: no pinned revid in the article JSON — reviewing "
              f"the legacy/live HTML", flush=True)

    if ctx.dry_run:
        posted, wire = finalize_for_post(existing, existing)
        meta = build_meta(ctx, {}, wire)
        print(f"  DRY-RUN {slug}: would POST base_version={base_version} "
              f"annotations={len(posted)} revid_reviewed={target_revid} "
              f"reason={job.get('reason')}\n"
              f"    meta={json.dumps(meta, ensure_ascii=False)}", flush=True)
        rec.update({"dry_run": True, "n_annotations": len(posted), "ids": wire})
        return rec

    arec = await ctx.ba.annotate_one({"title": title}, sem, ctx.seed_decls,
                                     moderate=True, existing_override=existing,
                                     target_revid=target_revid)
    rec.update(arec)
    if arec.get("error"):
        return rec
    # F14: surface harvested agent dissent on human annotations in the run log
    # (it also rides meta.ladder.moderation_flags via build_meta).
    for pair in (arec.get("ladder") or {}).get("moderation_flags") or []:
        fid, flag = (pair + [None, None])[:2] if isinstance(pair, list) else (None, pair)
        print(f"  ⚑ {slug}: moderation_flag on human annotation "
              f"[{fid}]: {flag}", flush=True)
    disk_slug = arec.get("slug") or slug
    if disk_slug != slug:
        print(f"  ! slug mismatch: D1 '{slug}' vs pipeline '{disk_slug}' "
              f"(reading disk artifact by pipeline slug, posting to D1 slug)",
              flush=True)
    try:
        final = json.loads(
            (ctx.ba.ANNOT / f"{disk_slug}.json").read_text())["annotations"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        rec["error"] = f"final_read_failed: {type(e).__name__}"
        return rec

    posted, wire = finalize_for_post(existing, final)
    rec["ids"] = wire
    body = {
        "annotations": posted,
        "base_version": base_version,
        "comment": f"ai-moderate:{ctx.mode}:{ctx.run_id}",
        "meta": build_meta(ctx, arec, wire),
    }
    code, resp = await post_article(ctx, slug, body)
    rec["post_status"] = code
    if code == 200:
        rec["posted_version"] = resp.get("version")
        rec["server_matched"] = resp.get("matched")
    elif code == 409:
        # Someone edited mid-run. Skip; /api/work re-queues it next run
        # (article.version > last_reviewed_version → 'human-edited' tier).
        rec["skipped"] = "stale_409"
        print(f"  409 {slug}: edited mid-run (server version "
              f"{resp.get('version')}) — skipped, re-queued next run", flush=True)
    elif code == 422:
        # BUG: finalize_for_post should make this impossible. Do NOT retry.
        rec["error"] = "human_lost_422"
        print(f"  *** 422 {slug}: HUMAN ANNOTATION LOST — finalize_for_post bug; "
              f"NOT retrying. missing={json.dumps(resp.get('missing'))}",
              file=sys.stderr, flush=True)
    elif code == 429:
        rec["error"] = "rate_limited_429"
    else:
        rec["error"] = f"post_{code}: {json.dumps(resp)[:200]}"
    return rec


async def process_new(article: dict, ctx, sem: asyncio.Semaphore) -> dict:
    """new: full disk pipeline (fetch → agents → render), then create the
    article in D1 via the bot-only PUT /api/article/:slug (contract D-C1).
    Dry-run: print the would-PUT summary, zero agent calls and zero writes."""
    slug, title = article["slug"], article["title"]
    rec: dict = {"slug": slug, "op": "new", "source": article.get("source")}
    qid = ctx.qid_map.get(title)
    if ctx.dry_run:
        revid = sidecar_revid(slug)
        print(f"  DRY-RUN new {slug}: would run the agent pipeline, then PUT "
              f"/api/article/{slug} with wikipedia_title={title!r}, "
              f"wikidata_qid={qid or '(none)'}, "
              f"revid={revid or '(from fetch sidecar)'}, "
              f"comment='ai-create:{ctx.run_id}'", flush=True)
        rec["dry_run"] = True
        return rec

    arec = await ctx.ba.annotate_one({"title": title}, sem, ctx.seed_decls,
                                     moderate=False)
    rec.update(arec)
    rec["op"] = "new"
    if arec.get("error"):
        return rec
    disk_slug = arec.get("slug") or slug
    if disk_slug != slug:
        print(f"  ! slug mismatch: candidate '{slug}' vs pipeline '{disk_slug}' "
              f"(reading disk artifact by pipeline slug, creating at D1 slug)",
              flush=True)
    try:
        envelope = json.loads((ctx.ba.ANNOT / f"{disk_slug}.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        rec["error"] = f"final_read_failed: {type(e).__name__}"
        return rec

    revid = sidecar_revid(disk_slug, Path(ctx.ba.CACHE))
    if revid is None:
        # F16: a create without a pinned revid would seed an article the
        # wp-update loop can never reason about — refuse the PUT, continue.
        rec["skipped"] = "no_revid"
        print(f"  {slug}: skipped (no revid) — fetch sidecar missing/invalid; "
              f"not creating", flush=True)
        return rec
    body, wire = build_create_body(
        envelope, revid=revid, wikidata_qid=qid, run_id=ctx.run_id)
    body["meta"] = build_meta(ctx, arec, wire)
    rec["ids"] = wire
    code, resp = await put_article(ctx, slug, body)
    rec["put_status"] = code
    if code == 201:
        rec["created_version"] = resp.get("version")
        rec["server_matched"] = "created"
    elif code == 409:
        # {error:'exists'} — created by another runner or an earlier (crashed)
        # pass. Not an error: log + skip; `review` owns it from here.
        rec["skipped"] = "exists_409"
        print(f"  409 {slug}: already exists in D1 — skipped "
              f"(review mode owns existing articles)", flush=True)
    elif code == 422:
        # Impossible on create (no stored annotations to lose) — a 422 here
        # means the server contract changed under us. Loud, no retry.
        rec["error"] = "create_422_impossible"
        print(f"  *** 422 {slug}: create returned the human-preservation "
              f"error, which cannot happen on a fresh slug — server contract "
              f"drift? body={json.dumps(resp)[:200]}", file=sys.stderr, flush=True)
    elif code == 429:
        rec["error"] = "rate_limited_429"
    else:
        rec["error"] = f"put_{code}: {json.dumps(resp)[:200]}"
    return rec


def probe_new_slugs(api_base: str, candidates: list[dict], limit: int,
                    token: str | None = None) -> list[dict]:
    """Keep candidates whose slug 404s on the article JSON endpoint — i.e.
    not yet in D1. Capped at MAX_NEW_PROBES probes. Sends the bearer header
    when available (F15); a tokenless dry-run probe still works (only the
    404-vs-200 distinction is read here)."""
    out: list[dict] = []
    probes = 0
    for a in candidates:
        if len(out) >= limit or probes >= MAX_NEW_PROBES:
            break
        probes += 1
        if probes % 50 == 0:
            print(f"  …probed {probes} candidates ({len(out)} new so far)", flush=True)
        r = _http_get(f"{api_base}/api/article/{urllib.parse.quote(a['slug'])}.json",
                      token)
        if r.status_code == 404:
            out.append(a)
        elif r.status_code != 200:
            print(f"  ! probe {a['slug']}: HTTP {r.status_code} — skipped "
                  f"(neither in D1 nor safely new)", flush=True)
    print(f"new candidates: {len(out)} (after {probes} probes)")
    return out


def find_new_candidates(api_base: str, limit: int,
                        token: str | None = None) -> list[dict]:
    """Catalog fallback: titles from ba.load_articles, 404-probed via
    probe_new_slugs. discover_articles.py output (--from-file) is the
    preferred feed — it diffs the LIVE WikiProject list against D1."""
    articles, _seed = ba.load_articles()
    cands = [{"title": a["title"], "slug": ba.make_slug(a["title"]),
              "source": "catalog"} for a in articles]
    return probe_new_slugs(api_base, cands, limit, token)


def run_wp_update(args, token: str | None) -> tuple[int, int]:
    """F3: drive update_from_upstream's stage-0 per-slug processing (the
    script stays usable standalone). Deterministic — zero agent tokens; in
    'all' mode this runs FIRST so the review queue, fetched fresh afterwards,
    no longer surfaces articles whose only problem was upstream drift."""
    import update_from_upstream as ufu
    jobs = fetch_work(args.api_base, token, "wp-update", args.limit)
    print(f"wp-update: {len(jobs)} drifted article(s) from /api/work"
          f"{'  [DRY RUN]' if args.dry_run else ''}")
    if not jobs:
        return 0, 0
    s = ufu.make_session()
    ns = SimpleNamespace(dry_run=args.dry_run, force_revid=None)
    results: list[dict] = []
    for i, job in enumerate(jobs):
        slug = job["slug"]
        print(f"  [{i + 1}/{len(jobs)}] {slug}", flush=True)
        try:
            rec = ufu.process_slug(s, args.api_base, slug, ns, args.run_id,
                                   lambda: token)
        except Exception as e:  # one bad slug must not kill the sweep
            rec = {"slug": slug, "outcome": f"error ({type(e).__name__}: {e})",
                   "matched": None, "total": None,
                   "old_revid": None, "new_revid": None}
        results.append(rec)
        print(f"      {rec['outcome']}", flush=True)
        if rec["outcome"] == "repinned" and i + 1 < len(jobs):
            time.sleep(ufu.WRITE_PACE_SECONDS)  # stay under EDIT_LIMITER
    n_repinned = sum(1 for r in results if r["outcome"] == "repinned")
    n_needs = sum(1 for r in results if r["outcome"] == "needs-work")
    print(f"wp-update: {len(results)} processed — {n_repinned} re-pinned, "
          f"{n_needs} need stage-1/2 (recorded in {ufu.REPORT_PATH})")
    return 0, 0


# ---------------------------------------------------------------------------
# Run loop (budget + consecutive-window-exhaustion abort, after ba.run)
# ---------------------------------------------------------------------------

async def run_jobs(jobs: list, ctx, process) -> tuple[int, int]:
    """Returns (exit_code, tokens_used). exit_code 3 = aborted (window/budget),
    matching batch_annotate.run's convention."""
    sem = asyncio.Semaphore(ctx.concurrency)
    t0 = time.time()
    state = {"consec_err": 0, "abort": False, "n_done": 0, "n_err": 0,
             "tokens": 0, "cost": 0.0}
    lock = asyncio.Lock()
    MODERATE_LOG.parent.mkdir(parents=True, exist_ok=True)

    with MODERATE_LOG.open("a", encoding="utf-8") as log:
        async def worker(job):
            if state["abort"]:
                return  # window/budget died — skip cheaply, retried next run
            try:
                rec = await process(job, ctx, sem)
            except Exception as e:
                # F12: one bad job/annotation must not kill the whole
                # asyncio.gather — log it and count it as a job error.
                slug = job.get("slug") if isinstance(job, dict) else None
                rec = {"slug": slug or "?",
                       "error": f"job_crashed: {type(e).__name__}: {e}"}
            async with lock:
                rec["run_id"] = ctx.run_id
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log.flush()
                state["n_done"] += 1
                state["tokens"] += rec.get("tokens") or 0
                state["cost"] += rec.get("cost_usd_equiv") or 0
                err = rec.get("error")
                if err:
                    state["n_err"] += 1
                    low = str(err).lower()
                    if ("error result: success" in low or "rate" in low
                            or "limit" in low or "overloaded" in low):
                        state["consec_err"] += 1
                        if state["consec_err"] >= ABORT_AFTER and not state["abort"]:
                            state["abort"] = True
                            print(f"  ⚠ {state['consec_err']} consecutive window-"
                                  f"exhaustion errors — aborting; rerun resumes "
                                  f"after the window resets", flush=True)
                else:
                    state["consec_err"] = 0
                if (ctx.budget_tokens and state["tokens"] >= ctx.budget_tokens
                        and not state["abort"]):
                    state["abort"] = True
                    print(f"  ⚠ token budget reached ({state['tokens']:,} >= "
                          f"{ctx.budget_tokens:,}) — aborting remaining jobs", flush=True)
                status = (rec.get("error") or rec.get("skipped")
                          or rec.get("server_matched")
                          or ("dry-run" if rec.get("dry_run") else "ok"))
                print(f"  [{state['n_done']}/{len(jobs)}] "
                      f"{str(rec.get('slug', '?'))[:40]:40s} {str(status):18s} "
                      f"err={state['n_err']} ~${state['cost']:.2f} equiv "
                      f"{state['tokens'] / 1e6:.2f}Mtok", flush=True)

        await asyncio.gather(*(worker(j) for j in jobs))

    print(f"{ctx.mode}: {state['n_done']} processed, {state['n_err']} errors, "
          f"{time.time() - t0:.0f}s, ~${state['cost']:.2f} equiv, "
          f"{state['tokens'] / 1e6:.2f}M tokens"
          + ("  [ABORTED — rerun to resume]" if state["abort"] else ""))
    return (3 if state["abort"] else 0), state["tokens"]


def make_ctx(args, mode: str, token: str | None, ba_mod) -> SimpleNamespace:
    return SimpleNamespace(
        mode=mode,
        api_base=args.api_base.rstrip("/"),
        token=token,
        auth=args.auth,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        budget_tokens=args.budget_tokens,
        run_id=args.run_id,
        ba=ba_mod,
        seed_decls=args.seed_decls,
        model=ba_mod.MODEL if ba_mod is not None else "unavailable",
        prompt_sha=get_prompt_sha(ba_mod, mode),
        mathlib_sha=get_mathlib_sha(),
    )


def run_mode(mode: str, args, token: str | None) -> tuple[int, int]:
    """One subcommand. Returns (exit_code, tokens_used)."""
    if mode == "wp-update":
        return run_wp_update(args, token)

    if mode == "review":
        ba_mod = _try_import_ba(args.auth) if args.dry_run else _import_ba(args.auth)
        # The job list is fetched HERE — i.e. in 'all' mode AFTER wp-update
        # has run, when stage-0 re-pins have already cleared wp_drifted for
        # zero tokens (F3: review spend goes to articles needing judgment).
        jobs = fetch_work(args.api_base, token, "review", args.limit)
        print(f"review: {len(jobs)} jobs from /api/work")
        if not jobs:
            return 0, 0
        ctx = make_ctx(args, "review", token, ba_mod)
        return asyncio.run(run_jobs(jobs, ctx, process_review))

    # mode == "new" — candidates from --from-file (discover_articles.py
    # output: slug precomputed, no ba needed to enumerate) or the catalog
    # fallback (needs ba.load_articles + ba.make_slug even for dry-run).
    if args.from_file:
        cands = load_candidate_file(Path(args.from_file))
        print(f"new: {len(cands)} candidates from {args.from_file}")
        candidates = probe_new_slugs(args.api_base, cands, args.limit, token)
        ba_mod = (_try_import_ba(args.auth) if args.dry_run
                  else _import_ba(args.auth))
    else:
        ba_mod = _import_ba(args.auth)
        candidates = find_new_candidates(args.api_base, args.limit, token)
    if not candidates:
        return 0, 0
    ctx = make_ctx(args, "new", token, ba_mod)
    ctx.qid_map = load_qid_map()
    return asyncio.run(run_jobs(candidates, ctx, process_new))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified D1-direct moderation runner (new | review | wp-update | all).")
    ap.add_argument("command", choices=["new", "review", "wp-update", "all"])
    ap.add_argument("--limit", type=int, default=10,
                    help="max articles per mode (default 10; /api/work caps at 100)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--budget-tokens", type=int, default=None,
                    help="abort the run once cumulative tokens reach this")
    ap.add_argument("--auth", choices=["subscription", "api-key"],
                    default="subscription",
                    help="subscription pops ANTHROPIC_API_KEY (Max-plan auth); "
                         "api-key leaves it so the SDK bills the API account")
    ap.add_argument("--dry-run", action="store_true",
                    help="no agent calls, no writes — print what would be written")
    ap.add_argument("--from-file", default=None, metavar="JSONL",
                    help="new mode: candidate JSONL from discover_articles.py "
                         "({'title','slug','source'} per line; default is the "
                         "catalog 404-probe fallback)")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = ap.parse_args()
    args.run_id = secrets.token_hex(4)
    args.seed_decls = {}
    args.api_base = args.api_base.rstrip("/")

    token = resolve_token()
    # The bearer token is needed for /api/work (review, wp-update) and all
    # writes — including `new`'s PUT creates. A dry `new` run only probes
    # public GETs, so it stays tokenless (donor-friendly smoke test).
    needs_token = (args.command in ("review", "wp-update", "all")
                   or (args.command == "new" and not args.dry_run))
    if token is None and needs_token:
        print("ERROR: no API token — set WIKILEAN_API_TOKEN or put "
              "PIPELINE_TOKEN= in wiki/.dev.vars", file=sys.stderr)
        return 1

    # F3: wp-update FIRST (zero-token stage-0 re-pins clear wp_drifted), then
    # review (job list fetched fresh afterwards — see module docstring), then new.
    modes = (["wp-update", "review", "new"] if args.command == "all"
             else [args.command])
    print(f"moderate run {args.run_id}: modes={modes} limit={args.limit} "
          f"auth={args.auth}{' DRY-RUN' if args.dry_run else ''} "
          f"api={args.api_base}")

    rc = 0
    budget_left = args.budget_tokens
    for mode in modes:
        if mode in ("review", "new") and not args.dry_run:
            # Mathlib seed-decl leads for Agent 2 (cheap disk read, load once).
            if not args.seed_decls:
                _import_ba(args.auth)
                _, args.seed_decls = ba.load_articles()
        args.budget_tokens = budget_left
        mode_rc, used = run_mode(mode, args, token)
        if budget_left is not None:
            budget_left = max(0, budget_left - used)
        rc = max(rc, mode_rc)
        if mode_rc == 3:
            print(f"aborted during {mode} — skipping remaining modes")
            break
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
