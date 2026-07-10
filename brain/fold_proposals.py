#!/usr/bin/env python3
"""Deterministic fold of the discovery-fleet proposals into brain/data.

Reads brain/proposals/*.jsonl (agent-proposed rows) together with their
*.verified.jsonl skeptic passes when present, re-applies hard machine checks to
EVERY row regardless of verdict, and emits only rows that survive:

  brain/data/container_links.jsonl      concept -> container formalizes links
  brain/data/discovery_proposals.jsonl  concept -> decl formalizes links
                                        (build_common's expected shape)
  brain/data/ext_anchor_links.jsonl     concept -> external-page anchors from the
                                        nightly sync_agents cartographer (action:"xref"
                                        rows); merge-deduped on (qid, db, id), _meta
                                        first line, tombstone-free append semantics
  brain/data/discovery_rejected.jsonl   every rejected row + reason (audit trail)
  catalog/data/grounding_overrides.jsonl   accepted override rows APPENDED
  catalog/data/universe_extension.jsonl    label/P31 rows for new QIDs APPENDED

Anti-slop invariants: a row without a skeptic verdict can still fold, but its
confidence is capped at "medium" and it carries skeptic:"pending" — the
precision class is published, never hidden. Deterministic checks (decl
existence oracle + checkout grep, hierarchy-path existence, live Wikidata
entity existence + label agreement) apply to all rows; failing rows are
rejected even if a skeptic accepted them.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = HERE / "data"
PROPOSALS = HERE / "proposals"
CATALOG = REPO / "catalog" / "data"
ORACLE = REPO / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"
CHECKOUT = Path(os.environ.get("BRAIN_MATHLIB_CHECKOUT",
                               "/Users/jack/Desktop/LEAN/mathlib4/Mathlib"))
UA = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"
QID_RE = re.compile(r"^Q\d+$")
CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


def oracle_names() -> set[str]:
    try:
        return set(json.loads(ORACLE.read_text()).get("declarations", {}))
    except (OSError, json.JSONDecodeError):
        return set()


def checkout_has(seg_decl: str) -> bool:
    """Same dotted-prefix pattern as build_graph_v2.checkout_has — the oracle
    cache is known-stale (misses real decls), so the checkout is the backstop."""
    kw = r"(theorem|lemma|def|abbrev|structure|class|instance|inductive)"
    seg = seg_decl.split(".")[-1]
    pat = f"{kw} +([A-Za-z0-9_'.«»]+\\.)?{re.escape(seg)}($|[^A-Za-z0-9_'])"
    try:
        r = subprocess.run(["grep", "-rIlE", pat, str(CHECKOUT)],
                           capture_output=True, text=True, timeout=30)
        return bool(r.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def hierarchy_paths() -> dict[str, int]:
    h = json.loads((CATALOG / "hierarchy.json").read_text())
    out: dict[str, int] = {}

    def walk(name: str, node: dict, prefix: str) -> None:
        p = f"{prefix}/{name}" if prefix else name
        out[p] = node.get("n_decls", 0)
        for k, v in (node.get("sub") or {}).items():
            walk(k, v, p)

    for lib, ln in h["libraries"].items():
        out[lib] = ln.get("n_decls", 0)
        for k, v in (ln.get("modules") or {}).items():
            walk(k, v, lib)
    return out


def crossref_dbs() -> set[str]:
    """source_registry.json crossref_sources keys — the only legal xref dbs
    (same contract as build_common.load_crossref_registry)."""
    try:
        reg = json.loads((CATALOG / "source_registry.json").read_text())
        return set(reg.get("crossref_sources", {}))
    except (OSError, json.JSONDecodeError):
        return set()


_ext_page_ids: dict[str, set[str] | None] = {}


def external_page_ids(db: str) -> set[str] | None:
    """Page ids in catalog/data/external/<db>_pages.jsonl (cached per db);
    None when the file does not exist (registry dbs without an ingest)."""
    if db not in _ext_page_ids:
        f = CATALOG / "external" / f"{db}_pages.jsonl"
        if not f.exists():
            _ext_page_ids[db] = None
        else:
            ids: set[str] = set()
            with f.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if "_meta" in r:
                        continue
                    if r.get("id") is not None:
                        ids.add(str(r["id"]))
            _ext_page_ids[db] = ids
    return _ext_page_ids[db]


def row_key(r: dict) -> tuple:
    """Identity of a proposal row — the join key between a base shard and its
    skeptic .verified.jsonl overlay. action:"xref" rows key on the (db, id)
    pair too: one QID can carry many external-page anchors, and without it
    every xref proposal on a QID would collide onto one key (skeptic overlay
    verdicts would cross-apply between different pages)."""
    x = r.get("xref") or {}
    return (r.get("qid"), r.get("decl") or r.get("new_decl"),
            r.get("path"), r.get("action"),
            x.get("db"), str(x["id"]) if x.get("id") is not None else None)


def known_qids() -> dict[str, dict]:
    """qid -> {label, aliases?} from the universe + extension (labels only)."""
    out: dict[str, dict] = {}
    for f in (CATALOG / "wikidata_universe.jsonl", CATALOG / "universe_extension.jsonl"):
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("qid"):
                out[r["qid"]] = r
    return out


def fetch_entities(qids: list[str]) -> dict[str, dict]:
    """wbgetentities in batches of 50: label/description/aliases/P31/sitelink.
    curl, not urllib: the system Python's SSL trust store is broken on this
    machine (same reason fetch_crossrefs.py / fetch_universe_extension.py shell
    out to curl)."""
    out: dict[str, dict] = {}
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               "&props=labels|descriptions|aliases|claims|sitelinks&languages=en"
               "&sitefilter=enwiki&ids=" + "|".join(chunk))
        r = subprocess.run(["curl", "-sS", "-m", "90", "--retry", "2",
                            "-H", f"User-Agent: {UA}", url],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not r.stdout.strip():
            print(f"WARNING: wbgetentities chunk {i//50} failed: {r.stderr.strip()[:200]}",
                  file=sys.stderr)
            continue
        ents = json.loads(r.stdout).get("entities", {})
        for qid, ent in ents.items():
            if "missing" in ent:
                out[qid] = {"missing": True}
                continue
            label = (ent.get("labels", {}).get("en") or {}).get("value")
            aliases = [a["value"] for a in ent.get("aliases", {}).get("en", [])]
            p31 = [c["mainsnak"]["datavalue"]["value"]["id"]
                   for c in ent.get("claims", {}).get("P31", [])
                   if c.get("mainsnak", {}).get("datavalue")]
            out[qid] = {
                "qid": ent.get("id", qid),  # redirects resolve to the target id
                "requested": qid,
                "label": label,
                "aliases": aliases,
                "description": (ent.get("descriptions", {}).get("en") or {}).get("value"),
                "classes": p31,
                "enwiki_slug": (ent.get("sitelinks", {}).get("enwiki") or {})
                .get("title", "").replace(" ", "_") or None,
            }
        time.sleep(1)
    return out


def main() -> int:
    paths = hierarchy_paths()
    oracle = oracle_names()
    # A missing oracle must FAIL, not degrade: an empty set would silently
    # reject every decl-bearing proposal (and in build_graph_v2's twin, drop
    # every formalization) on a machine without the gitignored cache.
    if not oracle:
        sys.exit(f"FATAL: decl oracle empty/missing at {ORACLE} — fetch it "
                 "(mathlib-search skill) before folding")
    if not CHECKOUT.exists():
        sys.exit(f"FATAL: mathlib checkout missing at {CHECKOUT} "
                 "(override with BRAIN_MATHLIB_CHECKOUT)")
    known = known_qids()
    grounding = {r["qid"]: r for r in json.loads((CATALOG / "rebuild_grounding.json").read_text())}

    # ---- collect rows: the BASE file is the row universe; the skeptic's
    # .verified.jsonl overlays verdicts onto it. Reading only the verified copy
    # would silently drop base rows a partial skeptic never echoed (found in
    # the 2026-07-03 self-review: a skeptic died mid-shard leaving 2/29 rows).
    rows: list[dict] = []
    n_unechoed = 0
    for f in sorted(glob.glob(str(PROPOSALS / "*.jsonl"))):
        if f.endswith(".verified.jsonl"):
            continue
        vf = Path(f + ".verified.jsonl")
        verdicts: dict[tuple, dict] = {}
        if vf.exists():
            for line in vf.read_text().splitlines():
                if line.strip():
                    v = json.loads(line)
                    verdicts[row_key(v)] = v
        seen = set()
        for line in Path(f).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            k = row_key(r)
            seen.add(k)
            v = verdicts.get(k)
            if v is not None:
                r = {**r, **{kk: v[kk] for kk in ("verdict", "verify_note") if kk in v}}
            elif vf.exists():
                n_unechoed += 1  # skeptic ran but never echoed this row → pending
            r["_shard"] = Path(f).name
            rows.append(r)
        # skeptic-added rows absent from the base (corrected copies) count too
        for k, v in verdicts.items():
            if k not in seen:
                v = dict(v)
                v["_shard"] = Path(f).name
                rows.append(v)
    if n_unechoed:
        print(f"NOTE: {n_unechoed} base rows had no skeptic echo — folded as "
              f"pending (capped confidence)", file=sys.stderr)

    # kind inference: container batches have path+no decl; collision rows have
    # action; discover rows have decl+qid
    def rtype(r: dict) -> str:
        if r.get("action"):
            return r["action"]
        if r.get("path") and not r.get("decl"):
            return "container"
        return "discover"

    # ---- live-fetch every not-yet-known QID ----------------------------------
    need = sorted({r["qid"] for r in rows
                   if r.get("qid") and QID_RE.match(r["qid"]) and r["qid"] not in known
                   and rtype(r) in ("container", "discover", "replace_decl", "xref")})
    fetched = fetch_entities(need) if need else {}
    print(f"fetched {len(fetched)}/{len(need)} unknown QIDs from Wikidata", file=sys.stderr)

    def qid_info(qid: str) -> dict | None:
        return known.get(qid) or fetched.get(qid)

    def label_agrees(r: dict, info: dict) -> bool:
        want = (r.get("qid_label") or r.get("label") or "").casefold().strip()
        if not want:
            return True  # container batches carry graph labels; no claim to check
        got = [(info.get("label") or "").casefold()] + \
              [a.casefold() for a in info.get("aliases", [])]
        return want in got or any(want == g for g in got)

    checkout_cache: dict[str, bool] = {}

    def decl_ok(d: str) -> bool:
        if d in oracle:
            return True
        if d not in checkout_cache:
            checkout_cache[d] = checkout_has(d)
        return checkout_cache[d]

    xref_dbs = crossref_dbs()
    containers_out: dict[tuple[str, str], dict] = {}
    discovery_out: dict[tuple[str, str], dict] = {}
    xref_out: dict[tuple[str, str, str], dict] = {}
    overrides_out: list[dict] = []
    rejected: list[dict] = []
    disputes: list[dict] = []
    n_ok = 0

    def reject(r: dict, why: str) -> None:
        rejected.append({**r, "rejected_reason": why})

    # Cross-batch reconciliation: proposers overlapped, so the same
    # (qid, target) pair can carry contradictory skeptic verdicts from
    # different shards. Any-reject wins — a link one skeptic refuted must not
    # ship because another batch's copy was accepted.
    vetoed: set[tuple] = set()
    for r in rows:
        if r.get("verdict") == "reject":
            t = rtype(r)
            if t == "container":
                vetoed.add(("container", r.get("qid"),
                            (r.get("path") or "").removeprefix("path:")))
            elif t in ("discover", "replace_decl"):
                vetoed.add(("discover", r.get("qid"),
                            r.get("decl") or r.get("new_decl")))
            elif t == "xref":
                x = r.get("xref") or {}
                vetoed.add(("xref", r.get("qid"), x.get("db"),
                            str(x["id"]) if x.get("id") is not None else None))

    for r in rows:
        t = rtype(r)
        verdict = r.get("verdict")
        if verdict == "reject":
            # A rejected 'ok' audit means the skeptic disputes an ALREADY-
            # SHIPPED grounding grade — that needs a correction surface, not a
            # silent drop. grading_disputes.jsonl feeds human review /
            # grounding_overrides.jsonl.
            if t == "ok":
                disputes.append({k: r.get(k) for k in
                                 ("qid", "decl", "note", "verify_note", "_shard")})
            reject(r, f"skeptic: {r.get('verify_note') or 'rejected'}")
            continue
        if t == "container" and ("container", r.get("qid"),
                                 (r.get("path") or "").removeprefix("path:")) in vetoed:
            reject(r, "fold-check: conflicting skeptic verdicts across batches "
                      "(any-reject wins)")
            continue
        if t in ("discover", "replace_decl") and \
                ("discover", r.get("qid"), r.get("decl") or r.get("new_decl")) in vetoed:
            reject(r, "fold-check: conflicting skeptic verdicts across batches "
                      "(any-reject wins)")
            continue
        if t == "xref":
            x = r.get("xref") or {}
            if ("xref", r.get("qid"), x.get("db"),
                    str(x["id"]) if x.get("id") is not None else None) in vetoed:
                reject(r, "fold-check: conflicting skeptic verdicts across batches "
                          "(any-reject wins)")
                continue
        skeptic = "accept" if verdict == "accept" else "pending"
        conf = r.get("confidence") or "medium"
        if skeptic == "pending" and CONF_ORDER.get(conf, 1) > CONF_ORDER["medium"]:
            conf = "medium"

        if t == "ok":
            n_ok += 1
            continue

        if t == "container":
            qid, path = r.get("qid"), (r.get("path") or "").removeprefix("path:")
            if not (qid and QID_RE.match(qid)):
                reject(r, "fold-check: bad qid")
                continue
            if path not in paths:
                reject(r, f"fold-check: path not in hierarchy.json: {path}")
                continue
            info = qid_info(qid)
            if info is None or info.get("missing"):
                reject(r, "fold-check: qid missing upstream")
                continue
            containers_out[(qid, path)] = {
                "qid": qid, "path": path, "match_kind": "field",
                "confidence": conf, "evidence": r.get("evidence"),
                "proposer": r.get("proposer"), "skeptic": skeptic,
            }
            continue

        if t == "override":
            # Overrides mutate already-shipped grades with no confidence
            # field to cap — unlike links, they apply only with an explicit
            # skeptic accept (the collision skeptics rejected ~half of
            # proposed overrides as no-ops or convention-inverted).
            if skeptic == "pending":
                reject(r, "fold-check: override requires a skeptic verdict — "
                          "left in proposals for the next skeptic pass")
                continue
            qid = r.get("qid")
            g = grounding.get(qid)
            if not g:
                reject(r, "fold-check: qid not in rebuild_grounding")
                continue
            decls = {f.get("decl") for f in (g.get("formalizations") or [])}
            bad = [k for k in (r.get("set") or {})
                   if k.startswith("match_kind:") and k.split(":", 1)[1] not in decls]
            if bad:
                reject(r, f"fold-check: override targets unknown decl(s) {bad}")
                continue
            overrides_out.append({"qid": qid, "set": r["set"],
                                  "reason": f"[{r.get('proposer')}|skeptic:{skeptic}] "
                                            f"{r.get('reason') or ''}".strip()})
            continue

        if t in ("discover", "replace_decl"):
            d = r.get("decl") or r.get("new_decl")
            qid = r.get("qid")
            if not (qid and QID_RE.match(qid)):
                reject(r, "fold-check: bad qid")
                continue
            if not d or not decl_ok(d):
                reject(r, f"fold-check: decl not found in oracle/checkout: {d}")
                continue
            info = qid_info(qid)
            if info is None or info.get("missing"):
                reject(r, "fold-check: qid missing upstream")
                continue
            if not label_agrees(r, info):
                reject(r, f"fold-check: label mismatch (upstream: {info.get('label')!r})")
                continue
            lib = "Mathlib"  # discovery fleets sweep the mathlib4 checkout only
            discovery_out[(qid, d)] = {
                "src": qid, "dst": f"decl:{lib}:{d}", "kind": "formalizes",
                "confidence": conf, "verified": True,
                "module": r.get("module"),
                "evidence": {"match_kind": r.get("match_kind") or "exact",
                             "note": r.get("evidence"),
                             "proposer": r.get("proposer"), "skeptic": skeptic},
            }
            continue

        if t == "xref":
            # sync_agents cartographer rows: anchor an external DB page to a
            # concept QID. Machine checks: db is a source_registry
            # crossref_sources key, the page id exists in the ingested
            # <db>_pages.jsonl, and the QID exists upstream with an agreeing
            # label — same live-Wikidata machinery as discover rows.
            # Like overrides (and unlike links), anchors NEVER fold on a
            # pending verdict: the machine checks are near-tautological for
            # dispatched candidates (page exists, QID exists), so the skeptic
            # is the only real gate against a prompt-injected cartographer.
            if skeptic == "pending":
                reject(r, "fold-check: ext anchor requires a skeptic verdict — "
                          "left in proposals for the next skeptic pass")
                continue
            x = r.get("xref") or {}
            db = x.get("db")
            pid = str(x["id"]) if x.get("id") is not None else None
            qid = r.get("qid")
            if not (qid and QID_RE.match(qid)):
                reject(r, "fold-check: bad qid")
                continue
            if not db or db not in xref_dbs:
                reject(r, f"fold-check: db not in source_registry "
                          f"crossref_sources: {db}")
                continue
            if not pid:
                reject(r, "fold-check: xref missing page id")
                continue
            page_ids = external_page_ids(db)
            if page_ids is None:
                reject(r, f"fold-check: no ingested pages file "
                          f"catalog/data/external/{db}_pages.jsonl")
                continue
            if pid not in page_ids:
                reject(r, f"fold-check: page id not in "
                          f"catalog/data/external/{db}_pages.jsonl: {pid}")
                continue
            info = qid_info(qid)
            if info is None or info.get("missing"):
                reject(r, "fold-check: qid missing upstream")
                continue
            if not label_agrees(r, info):
                reject(r, f"fold-check: label mismatch (upstream: {info.get('label')!r})")
                continue
            evidence = {"title": r.get("title"), "url": r.get("url"),
                        "reason": r.get("reason"), "proposer": r.get("proposer"),
                        "skeptic": skeptic, "shard": r.get("_shard")}
            xref_out[(qid, db, pid)] = {
                "qid": qid, "db": db, "id": pid, "confidence": conf,
                "evidence": {k: v for k, v in evidence.items() if v is not None},
            }
            continue

        reject(r, f"fold-check: unknown row type {t!r}")

    # ---- writes ---------------------------------------------------------------
    def dump(path: Path, rows_: list[dict]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows_))
        tmp.replace(path)

    dump(DATA / "container_links.jsonl", [containers_out[k] for k in sorted(containers_out)])
    dump(DATA / "discovery_proposals.jsonl", [discovery_out[k] for k in sorted(discovery_out)])
    dump(DATA / "discovery_rejected.jsonl", rejected)
    dump(DATA / "grading_disputes.jsonl", disputes)

    # ext-anchor links: regenerated from ALL verified proposals each fold, then
    # merge-deduped with rows already in the file (shards may be archived
    # later). RETRACTION: a key rejected THIS fold (skeptic refutation,
    # conflicting verdicts, failed machine check) is dropped from the merged
    # file too — otherwise a live anchor could never be withdrawn (the
    # any-reject veto already keeps it out of xref_out).
    retract: set[tuple[str, str, str]] = set()
    for r in rejected:
        if rtype(r) == "xref":
            x = r.get("xref") or {}
            if r.get("qid") and x.get("db") and x.get("id") is not None:
                retract.add((r["qid"], x["db"], str(x["id"])))
    xa_path = DATA / "ext_anchor_links.jsonl"
    n_xref = 0
    if xref_out or xa_path.exists():
        merged: dict[tuple[str, str, str], dict] = {}
        if xa_path.exists():
            for line in xa_path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    continue
                if row.get("qid") and row.get("db") and row.get("id") is not None:
                    key = (row["qid"], row["db"], str(row["id"]))
                    if key not in retract:
                        merged[key] = row
        merged.update(xref_out)
        n_xref = len(merged)
        meta = {"_meta": {"source": "brain/fold_proposals.py",
                          "inputs": "brain/proposals/ext_anchor_*.jsonl",
                          "n_rows": n_xref}}
        dump(xa_path, [meta] + [merged[k] for k in sorted(merged)])

    ov_path = CATALOG / "grounding_overrides.jsonl"
    existing = set()
    if ov_path.exists():
        for line in ov_path.read_text().splitlines():
            if line.strip():
                o = json.loads(line)
                existing.add((o.get("qid"), json.dumps(o.get("set"), sort_keys=True)))
    added_ov = 0
    with ov_path.open("a") as fh:
        for o in overrides_out:
            key = (o["qid"], json.dumps(o["set"], sort_keys=True))
            if key in existing:
                continue
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")
            existing.add(key)
            added_ov += 1

    ext_path = CATALOG / "universe_extension.jsonl"
    have = set(known)
    if ext_path.exists():
        for line in ext_path.read_text().splitlines():
            if line.strip():
                have.add(json.loads(line).get("qid"))
    added_ext = 0
    accepted_qids = {k[0] for k in containers_out} | {k[0] for k in discovery_out} \
        | {k[0] for k in xref_out}
    with ext_path.open("a") as fh:
        for qid in sorted(accepted_qids):
            info = fetched.get(qid)
            if not info or info.get("missing") or qid in have:
                continue
            fh.write(json.dumps({
                "qid": qid, "label": info.get("label"),
                "description": info.get("description"),
                "classes": info.get("classes"), "enwiki_slug": info.get("enwiki_slug"),
                "source": "discovery",
            }, ensure_ascii=False) + "\n")
            have.add(qid)
            added_ext += 1

    n_pending = sum(1 for v in list(containers_out.values()) + list(discovery_out.values())
                    + list(xref_out.values())
                    if (v.get("skeptic") or v["evidence"].get("skeptic")) == "pending")
    print(f"folded: {len(containers_out)} container links, {len(discovery_out)} discovery "
          f"links, {len(xref_out)} ext-anchor links ({n_xref} total in file), "
          f"{added_ov} new overrides, {added_ext} universe-extension rows; "
          f"{n_ok} ok-confirmations; {len(rejected)} rejected; "
          f"{len(disputes)} grading disputes (review → grounding_overrides.jsonl); "
          f"{n_pending} rows carry skeptic:pending (capped at medium confidence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
