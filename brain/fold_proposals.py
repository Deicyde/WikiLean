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
  catalog/data/<key>_links.jsonl        concept -> frontier-repo decl agent joins
                                        (action:"repo_link" rows, one file per
                                        source_registry frontier_sources key, e.g.
                                        tauceti_links.jsonl); MENTIONS-ONLY — this
                                        channel can never mint an identity-strength
                                        kind (moderation contract; see the repo_link
                                        branch below)
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

from build_context import external_pair_control_paths
from ingest.common import read_stable_external_pair

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = HERE / "data"
PROPOSALS = HERE / "proposals"
CATALOG = REPO / "catalog" / "data"
ORACLE = REPO / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"
CHECKOUT = Path(os.environ.get("BRAIN_MATHLIB_CHECKOUT",
                               "/Users/jack/Desktop/LEAN/mathlib4/Mathlib"))
UA = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"
QID_RE = re.compile(r"^Q[1-9]\d*$")
CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


class WikidataAcquisitionError(RuntimeError):
    """The live Wikidata batch could not be acquired completely and parsed."""


def is_qid(value: object) -> bool:
    """Whether value is a canonical, positive Wikidata item identifier."""
    return isinstance(value, str) and QID_RE.fullmatch(value) is not None


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


# Module resolution for discover rows that arrive without one (a 2026-07-18
# fleet shipped 150+ verified joins whose cells then fell to the path:Mathlib
# ROOT — real decl, no module, no supercell). Oracle first (docLink encodes the
# module), single-hit checkout grep as backstop; an ambiguous suffix returns
# None rather than guessing (the bare-suffix trap in mathlib_decl_oracles).
_oracle_modules: dict[str, str] | None = None


def oracle_module(decl: str) -> str | None:
    global _oracle_modules
    if _oracle_modules is None:
        _oracle_modules = {}
        try:
            for n, v in json.loads(ORACLE.read_text()).get("declarations", {}).items():
                m = re.match(r"^\./(.+)\.html(?:#|$)", (v or {}).get("docLink") or "")
                if m:
                    _oracle_modules[n] = m.group(1).replace("/", ".")
        except (OSError, json.JSONDecodeError):
            pass
    return _oracle_modules.get(decl)


def checkout_module(seg_decl: str) -> str | None:
    kw = r"(theorem|lemma|def|abbrev|structure|class|instance|inductive)"
    seg = seg_decl.split(".")[-1]
    pat = f"{kw} +([A-Za-z0-9_'.«»]+\\.)?{re.escape(seg)}($|[^A-Za-z0-9_'])"
    try:
        r = subprocess.run(["grep", "-rIlE", pat, str(CHECKOUT)],
                           capture_output=True, text=True, timeout=30)
        files = [f for f in r.stdout.splitlines() if f.endswith(".lean")]
    except (subprocess.SubprocessError, OSError):
        return None
    if len(files) != 1:
        return None
    rel = os.path.relpath(files[0], str(CHECKOUT))
    return rel[:-len(".lean")].replace("/", ".")


def resolve_module(decl: str) -> str | None:
    return oracle_module(decl) or checkout_module(decl)


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
            links = f.parent / f"{db}_links.jsonl"
            journal = external_pair_control_paths(f.parent, db)["journal"]
            if links.exists() or journal.exists():
                # A first-publication crash may leave a sealed links orphan.
                # The stable reader must reject it rather than treating the
                # source as cleanly absent.
                read_stable_external_pair(db, f, links)
            _ext_page_ids[db] = None
        else:
            _meta, rows, _links_meta, _links = read_stable_external_pair(
                db,
                f,
                f.parent / f"{db}_links.jsonl",
            )
            ids = {
                str(row["id"])
                for row in rows
                if row.get("id") is not None
            }
            _ext_page_ids[db] = ids
    return _ext_page_ids[db]


def row_key(r: dict) -> tuple:
    """Identity of a proposal row — the join key between a base shard and its
    skeptic .verified.jsonl overlay. action:"xref" rows key on the (db, id)
    pair too: one QID can carry many external-page anchors, and without it
    every xref proposal on a QID would collide onto one key (skeptic overlay
    verdicts would cross-apply between different pages). repo_link rows key on
    `repo` too (decl names are repo-namespaced, but the key must not depend on
    that convention)."""
    x = r.get("xref") or {}
    return (r.get("qid"), r.get("decl") or r.get("new_decl"),
            r.get("path"), r.get("action"), r.get("repo"),
            x.get("db"), str(x["id"]) if x.get("id") is not None else None)


REPO_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_frontier_names: dict[str, set[str] | None] = {}


def frontier_repo_keys() -> set[str]:
    """source_registry.json frontier_sources keys — the only legal `repo`
    values on repo_link rows (same single-source-of-truth contract as
    crossref_dbs())."""
    try:
        reg = json.loads((CATALOG / "source_registry.json").read_text())
        return set(reg.get("frontier_sources", {}))
    except (OSError, json.JSONDecodeError):
        return set()


def frontier_decl_names(key: str) -> set[str] | None:
    """FQ decl names of a frontier-repo harvest — the existence oracle for
    fc_link / repo_link rows. catalog/data/<key>.jsonl (first line _meta),
    except user_lean_repos whose harvests are the catalog/data/user_repos/
    glob. None when the harvest is absent (rows then reject with a clear
    reason). Cached per key."""
    if key in _frontier_names:
        return _frontier_names[key]
    if key == "user_lean_repos":
        files = sorted((CATALOG / "user_repos").glob("*.jsonl"))
    else:
        f = CATALOG / f"{key}.jsonl"
        files = [f] if f.exists() else []
    if not files:
        _frontier_names[key] = None
        return None
    names: set[str] = set()
    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "_meta" not in r and r.get("decl"):
                    names.add(r["decl"])
    _frontier_names[key] = names
    return names


def fc_decl_names() -> set[str] | None:
    """Thin wrapper: the fc_link oracle is the formal_conjectures harvest."""
    return frontier_decl_names("formal_conjectures")


def _hashable_key(value: object) -> object:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return repr(value)


def _completed_retract_key(
    qid: object,
    decl: object,
    names: set[str] | None,
) -> tuple[object, object]:
    """Return the same identity used by a successfully folded declaration row.

    Malformed rows are left unchanged so normal validation can reject them later;
    veto collection must not crash before producing the audit rejection.
    """
    if isinstance(decl, str) and names and decl not in names:
        cands = [n for n in names if n.endswith("." + decl)]
        if len(cands) == 1:
            decl = cands[0]
    # Veto identities must remain hashable even for malformed external rows.
    # JSON canonicalization is deterministic and preserves the invalid value for
    # the later audit rejection without letting it abort the whole fold.
    return (_hashable_key(qid), _hashable_key(decl))


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


def _fetch_entity_chunk(chunk: list[str], chunk_number: int) -> dict[str, dict]:
    """Fetch one logical chunk, bisecting only Wikidata no-such-entity errors."""
    url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
           "&props=labels|descriptions|aliases|claims|sitelinks&languages=en"
           "&sitefilter=enwiki&redirects=yes&ids=" + "|".join(chunk))
    try:
        response = subprocess.run(
            ["curl", "-sS", "-m", "90", "--retry", "2",
             "-H", f"User-Agent: {UA}", url],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} request failed "
            f"({type(exc).__name__})"
        ) from exc
    if response.returncode != 0:
        detail = " ".join((response.stderr or "").split())[:200] or "no stderr"
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} curl exited "
            f"{response.returncode}: {detail}"
        )
    # Throttle every completed request, including malformed/API-error
    # responses that will be retried through no-such-entity bisection.
    time.sleep(1)
    if not response.stdout.strip():
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} returned an empty response"
        )
    try:
        payload = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} response is not an object"
        )

    error = payload.get("error")
    if error is not None:
        if isinstance(error, dict) and error.get("code") == "no-such-entity":
            # wbgetentities rejects the entire multi-ID request when even one
            # syntactically valid, positive QID is beyond Wikidata's range.
            # Bisect to retain valid peers and represent only isolated bad IDs
            # as missing.  Every other API error remains fatal.
            if len(chunk) == 1:
                return {chunk[0]: {"missing": True}}
            midpoint = len(chunk) // 2
            left = _fetch_entity_chunk(chunk[:midpoint], chunk_number)
            right = _fetch_entity_chunk(chunk[midpoint:], chunk_number)
            return {**left, **right}
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} returned an API error"
        )

    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} has no entities object"
        )
    redirect_rows = payload.get("redirects", [])
    if not isinstance(redirect_rows, list):
        raise WikidataAcquisitionError(
            f"wbgetentities chunk {chunk_number} redirects is not a list"
        )
    redirects: dict[str, str] = {}
    for row in redirect_rows:
        if not isinstance(row, dict) or not is_qid(row.get("from")) \
                or not is_qid(row.get("to")):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} has a malformed redirect"
            )
        source, target = row["from"], row["to"]
        if source in redirects and redirects[source] != target:
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} has conflicting redirects"
            )
        redirects[source] = target

    out: dict[str, dict] = {}
    for requested in chunk:
        resolved = requested
        seen: set[str] = set()
        while resolved in redirects:
            if resolved in seen:
                raise WikidataAcquisitionError(
                    f"wbgetentities chunk {chunk_number} has a redirect cycle"
                )
            seen.add(resolved)
            resolved = redirects[resolved]
        entity = entities.get(requested)
        if entity is None:
            entity = entities.get(resolved)
        if entity is None:
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} omitted requested QID {requested}"
            )
        if not isinstance(entity, dict):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} is not an object"
            )
        if "missing" in entity:
            out[requested] = {"missing": True}
            continue

        entity_qid = entity.get("id", resolved)
        labels = entity.get("labels", {})
        aliases_by_language = entity.get("aliases", {})
        descriptions = entity.get("descriptions", {})
        claims = entity.get("claims", {})
        sitelinks = entity.get("sitelinks", {})
        if not is_qid(entity_qid) or not isinstance(labels, dict) \
                or not isinstance(aliases_by_language, dict) \
                or not isinstance(descriptions, dict) \
                or not isinstance(claims, dict) \
                or not isinstance(sitelinks, dict):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} is malformed"
            )

        label_row = labels.get("en")
        if label_row is not None and (not isinstance(label_row, dict)
                                      or not isinstance(label_row.get("value"), str)):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} has a malformed label"
            )
        alias_rows = aliases_by_language.get("en", [])
        if not isinstance(alias_rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("value"), str)
            for row in alias_rows
        ):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} has malformed aliases"
            )
        description_row = descriptions.get("en")
        if description_row is not None and (
            not isinstance(description_row, dict)
            or not isinstance(description_row.get("value"), str)
        ):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} has a malformed description"
            )
        p31_rows = claims.get("P31", [])
        if not isinstance(p31_rows, list):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} has malformed P31 claims"
            )
        p31: list[str] = []
        for claim in p31_rows:
            if not isinstance(claim, dict) or not isinstance(claim.get("mainsnak"), dict):
                raise WikidataAcquisitionError(
                    f"wbgetentities chunk {chunk_number} entity {requested} "
                    "has a malformed P31 claim"
                )
            datavalue = claim["mainsnak"].get("datavalue")
            if datavalue is None:
                continue
            if not isinstance(datavalue, dict) or not isinstance(datavalue.get("value"), dict) \
                    or not is_qid(datavalue["value"].get("id")):
                raise WikidataAcquisitionError(
                    f"wbgetentities chunk {chunk_number} entity {requested} "
                    "has a malformed P31 value"
                )
            p31.append(datavalue["value"]["id"])
        enwiki = sitelinks.get("enwiki")
        if enwiki is not None and (not isinstance(enwiki, dict)
                                   or not isinstance(enwiki.get("title"), str)):
            raise WikidataAcquisitionError(
                f"wbgetentities chunk {chunk_number} entity {requested} has a malformed sitelink"
            )
        out[requested] = {
            "qid": entity_qid,
            "requested": requested,
            "label": label_row["value"] if label_row is not None else None,
            "aliases": [row["value"] for row in alias_rows],
            "description": description_row["value"] if description_row is not None else None,
            "classes": p31,
            "enwiki_slug": enwiki["title"].replace(" ", "_") if enwiki else None,
        }
    return out


def fetch_entities(qids: list[str]) -> dict[str, dict]:
    """Fetch complete Wikidata entity evidence in deterministic batches of 50.

    Acquisition is all-or-nothing except that an isolated canonical QID which
    Wikidata reports as no-such-entity is represented as ``missing``.  A failed,
    malformed, or incomplete request raises before folding can publish outputs.
    """
    if any(not is_qid(qid) for qid in qids):
        raise WikidataAcquisitionError("wbgetentities received a non-canonical QID")
    out: dict[str, dict] = {}
    for i in range(0, len(qids), 50):
        out.update(_fetch_entity_chunk(qids[i:i + 50], i // 50))
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

    checkout_cache: dict[str, bool] = {}

    def decl_ok(d: str) -> bool:
        if d in oracle:
            return True
        if d not in checkout_cache:
            checkout_cache[d] = checkout_has(d)
        return checkout_cache[d]

    xref_dbs = crossref_dbs()
    fc_names = fc_decl_names()
    repo_keys = frontier_repo_keys()

    def veto_key(r: dict, t: str) -> tuple | None:
        """Return the exact any-reject identity used by the fold loop."""
        if t == "container":
            path = r.get("path")
            normalized = path.removeprefix("path:") \
                if isinstance(path, str) else _hashable_key(path)
            return ("container", _hashable_key(r.get("qid")), normalized)
        if t in ("discover", "replace_decl"):
            return ("discover", _hashable_key(r.get("qid")),
                    _hashable_key(r.get("decl") or r.get("new_decl")))
        if t == "xref":
            xref = r.get("xref") if isinstance(r.get("xref"), dict) else {}
            return ("xref", _hashable_key(r.get("qid")),
                    _hashable_key(xref.get("db")),
                    str(xref["id"]) if xref.get("id") is not None else None)
        if t == "fc_link":
            qid, decl = _completed_retract_key(r.get("qid"), r.get("decl"), fc_names)
            return ("fc_link", qid, decl)
        if t == "repo_link":
            repo = r.get("repo")
            names = frontier_decl_names(repo) \
                if isinstance(repo, str) and repo in repo_keys else None
            qid, decl = _completed_retract_key(r.get("qid"), r.get("decl"), names)
            return ("repo_link", _hashable_key(repo), qid, decl)
        return None

    # Cross-batch reconciliation is computed before acquisition.  A rejected
    # row and every accepted duplicate covered by its any-reject veto are audit
    # outputs only; they must not make the fold depend on a live upstream QID.
    vetoed = {
        key
        for r in rows
        if r.get("verdict") == "reject"
        for key in [veto_key(r, rtype(r))]
        if key is not None
    }

    def claimed_label(r: dict) -> object:
        return r.get("qid_label") or r.get("label") or ""

    def locally_fetchable(r: dict, t: str) -> bool:
        """Whether this row can reach an upstream-dependent fold check.

        This mirrors only deterministic checks that precede qid_info() in the
        branch below.  It is intentionally conservative: uncertain rows fetch;
        rows guaranteed to reject locally do not create a network dependency.
        """
        if not isinstance(t, str) or t not in {
            "container", "discover", "replace_decl", "xref", "fc_link", "repo_link",
        }:
            return False
        qid = r.get("qid")
        if not is_qid(qid) or qid in known or r.get("verdict") == "reject":
            return False
        key = veto_key(r, t)
        if key is not None and key in vetoed:
            return False
        skeptic = "accept" if r.get("verdict") == "accept" else "pending"
        if t == "container":
            path = r.get("path")
            return isinstance(path, str) and path.removeprefix("path:") in paths
        if not isinstance(claimed_label(r), str):
            return False
        if t in ("discover", "replace_decl"):
            decl = r.get("decl") or r.get("new_decl")
            return isinstance(decl, str) and bool(decl) and decl_ok(decl)
        if t == "xref":
            xref = r.get("xref")
            if skeptic != "accept" or not isinstance(xref, dict):
                return False
            db = xref.get("db")
            page_id = str(xref["id"]) if xref.get("id") is not None else None
            if not isinstance(db, str) or db not in xref_dbs or not page_id:
                return False
            page_ids = external_page_ids(db)
            return page_ids is not None and page_id in page_ids
        if t == "fc_link":
            decl, kind = r.get("decl"), r.get("kind")
            if kind not in ("formalizes", "mentions") or fc_names is None \
                    or not isinstance(decl, str):
                return False
            if decl not in fc_names:
                matches = [name for name in fc_names if name.endswith("." + decl)]
                if len(matches) != 1:
                    return False
            if kind == "formalizes" and (skeptic == "pending"
                                          or (r.get("match_kind") or "exact") != "exact"):
                return False
            return True
        repo, decl = r.get("repo"), r.get("decl")
        if not isinstance(repo, str) or not REPO_KEY_RE.match(repo) \
                or repo not in repo_keys or r.get("kind") != "mentions" \
                or not isinstance(decl, str) \
                or not isinstance(r.get("evidence"), str) or not r["evidence"].strip() \
                or not isinstance(r.get("qid_label"), str) or not r["qid_label"].strip():
            return False
        names = frontier_decl_names(repo)
        if names is None:
            return False
        return decl in names or sum(name.endswith("." + decl) for name in names) == 1

    # ---- live-fetch every locally admissible, not-yet-known QID ---------------
    need = sorted({r["qid"] for r in rows if locally_fetchable(r, rtype(r))})
    try:
        fetched = fetch_entities(need) if need else {}
    except WikidataAcquisitionError as exc:
        print(f"FATAL: Wikidata acquisition failed: {exc}", file=sys.stderr)
        return 1
    absent = [qid for qid in need if qid not in fetched]
    if absent:
        # Keep the fold boundary fail-closed even if fetch_entities is replaced
        # by a test double or future acquisition implementation.
        print(f"FATAL: Wikidata acquisition returned only "
              f"{len(fetched)}/{len(need)} requested QIDs; no outputs written",
              file=sys.stderr)
        return 1
    print(f"fetched {len(fetched)}/{len(need)} unknown QIDs from Wikidata", file=sys.stderr)

    def qid_info(qid: str) -> dict | None:
        return known.get(qid) or fetched.get(qid)

    def label_agrees(r: dict, info: dict) -> bool:
        raw_label = claimed_label(r)
        if not isinstance(raw_label, str):
            return False
        want = raw_label.casefold().strip()
        if not want:
            return True  # container batches carry graph labels; no claim to check
        got = [(info.get("label") or "").casefold()] + \
              [a.casefold() for a in info.get("aliases", [])]
        return want in got or any(want == g for g in got)

    containers_out: dict[tuple[str, str], dict] = {}
    discovery_out: dict[tuple[str, str], dict] = {}
    xref_out: dict[tuple[str, str, str], dict] = {}
    fc_out: dict[tuple[str, str], dict] = {}
    repo_out: dict[str, dict[tuple[str, str], dict]] = {}  # key -> (qid, decl)
    overrides_out: list[dict] = []
    rejected: list[dict] = []
    disputes: list[dict] = []
    n_ok = 0

    def reject(r: dict, why: str) -> None:
        rejected.append({**r, "rejected_reason": why})

    for r in rows:
        t = rtype(r)
        verdict = r.get("verdict")
        qid_value = r.get("qid")
        if not isinstance(t, str):
            reject(r, f"fold-check: unknown row type {t!r}")
            continue
        if t in {"container", "discover", "replace_decl", "xref", "fc_link", "repo_link"} \
                and not isinstance(qid_value, str):
            reject(r, "fold-check: qid must be a string")
            continue
        if t == "container" and not isinstance(r.get("path"), str):
            reject(r, "fold-check: path must be a string")
            continue
        if t in {"discover", "replace_decl", "fc_link", "repo_link"} and not isinstance(
            r.get("decl") or r.get("new_decl"), str
        ):
            reject(r, "fold-check: decl must be a string")
            continue
        if t == "xref" and not isinstance(r.get("xref"), dict):
            reject(r, "fold-check: xref must be an object")
            continue
        if t == "repo_link" and not isinstance(r.get("repo"), str):
            reject(r, "fold-check: repo must be a string")
            continue
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
        if t == "container" and veto_key(r, t) in vetoed:
            reject(r, "fold-check: conflicting skeptic verdicts across batches "
                      "(any-reject wins)")
            continue
        if t in ("discover", "replace_decl") and veto_key(r, t) in vetoed:
            reject(r, "fold-check: conflicting skeptic verdicts across batches "
                      "(any-reject wins)")
            continue
        if t == "xref":
            if veto_key(r, t) in vetoed:
                reject(r, "fold-check: conflicting skeptic verdicts across batches "
                          "(any-reject wins)")
                continue
        if t == "fc_link":
            if veto_key(r, t) in vetoed:
                reject(r, "fold-check: conflicting skeptic verdicts across batches "
                          "(any-reject wins)")
                continue
        if t == "repo_link":
            if veto_key(r, t) in vetoed:
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
            qid, raw_path = r.get("qid"), r.get("path")
            path = raw_path.removeprefix("path:") if isinstance(raw_path, str) else None
            if not is_qid(qid):
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
            if not is_qid(qid):
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
                "module": r.get("module") or resolve_module(d),
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
            x = r.get("xref")
            if not isinstance(x, dict):
                reject(r, "fold-check: xref must be an object")
                continue
            db = x.get("db")
            pid = str(x["id"]) if x.get("id") is not None else None
            qid = r.get("qid")
            if not is_qid(qid):
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

        if t == "fc_link":
            # fc-tagger fleet rows: join a Wikidata concept to a
            # decl:FormalConjectures:* declaration. Existence oracle = the
            # deterministic harvest (catalog/data/formal_conjectures.jsonl);
            # QID checks are the same live-Wikidata machinery as discover
            # rows. Policy mirrors xref/override for the STRONG kind:
            # formalizes NEVER folds on a pending verdict (a wrong "exact"
            # welds two atoms downstream); mentions folds pending at capped
            # medium like discover rows.
            qid, d, kind = r.get("qid"), r.get("decl"), r.get("kind")
            if not is_qid(qid):
                reject(r, "fold-check: bad qid")
                continue
            if kind not in ("formalizes", "mentions"):
                reject(r, f"fold-check: bad fc_link kind {kind!r}")
                continue
            if fc_names is None:
                reject(r, "fold-check: formal-conjectures harvest missing "
                          "(run brain/ingest/formal_conjectures.py)")
                continue
            d_orig = d
            if not isinstance(d, str):
                reject(r, "fold-check: decl must be a string")
                continue
            if d and d not in fc_names:
                # taggers sometimes drop the file's top-level namespace
                # (erdos_1095.variants.x for Erdos1095.erdos_1095.variants.x).
                # Complete it ONLY on a unique dotted-suffix match — exact
                # boundary, one candidate in the 4k-name harvest; ambiguity
                # still rejects (never the bare-suffix-guess trap).
                cands = [n for n in fc_names if n.endswith("." + d)]
                if len(cands) == 1:
                    d = cands[0]
            if not d or d not in fc_names:
                reject(r, f"fold-check: decl not in the formal-conjectures "
                          f"harvest: {d}")
                continue
            if kind == "formalizes" and skeptic == "pending":
                reject(r, "fold-check: fc_link formalizes requires a skeptic "
                          "verdict — left in proposals for the next skeptic pass")
                continue
            info = qid_info(qid)
            if info is None or info.get("missing"):
                reject(r, "fold-check: qid missing upstream")
                continue
            if not label_agrees(r, info):
                reject(r, f"fold-check: label mismatch (upstream: {info.get('label')!r})")
                continue
            mk = r.get("match_kind") or "exact"
            if kind == "formalizes" and mk != "exact":
                reject(r, f"fold-check: fc_link formalizes must be match_kind "
                          f"exact, got {mk!r}")
                continue
            evidence = {"note": r.get("evidence"), "url": r.get("url"),
                        "wikipedia_slug": r.get("wikipedia_slug"),
                        "file": r.get("file"), "proposer": r.get("proposer"),
                        "skeptic": skeptic, "shard": r.get("_shard"),
                        "decl_as_proposed": d_orig if d != d_orig else None}
            row_out = {
                "qid": qid, "decl": d, "kind": kind, "confidence": conf,
                "evidence": {k: v for k, v in evidence.items() if v is not None},
            }
            if kind == "formalizes":
                row_out["match_kind"] = mk
            fc_out[(qid, d)] = row_out
            continue

        if t == "repo_link":
            # Generic frontier-repo agent joins (the fc_link channel
            # parameterized by source_registry frontier_sources key): join a
            # Wikidata concept to a decl:<Lib>:* declaration of any
            # git-harvested Lean repo. Existence oracle = that repo's harvest
            # (catalog/data/<key>.jsonl); QID checks are the same
            # live-Wikidata machinery as discover rows. MODERATION CONTRACT
            # (hard): this channel folds MENTIONS ONLY — never a kind
            # build_cells fuses cells on (formalizes/exact identity claims
            # need human review; FC's gated formalizes path is FC-only and
            # separate). Mentions fold pending at capped medium, like
            # discover rows.
            key, qid, d = r.get("repo"), r.get("qid"), r.get("decl")
            if not (isinstance(key, str) and REPO_KEY_RE.match(key)
                    and key in repo_keys):
                reject(r, f"fold-check: repo not a source_registry "
                          f"frontier_sources key: {key!r}")
                continue
            if r.get("kind") != "mentions":
                reject(r, f"fold-check: repo_link folds kind 'mentions' only "
                          f"(AI joins never mint identity claims — moderation "
                          f"contract), got {r.get('kind')!r}")
                continue
            if not is_qid(qid):
                reject(r, "fold-check: bad qid")
                continue
            if not isinstance(d, str):
                reject(r, "fold-check: decl must be a string")
                continue
            if not isinstance(r.get("evidence"), str) or not r["evidence"].strip():
                reject(r, "fold-check: repo_link requires evidence text")
                continue
            if not (r.get("qid_label") or "").strip():
                reject(r, "fold-check: repo_link requires qid_label "
                          "(the label-agreement gate needs a claim to check)")
                continue
            names = frontier_decl_names(key)
            if names is None:
                reject(r, f"fold-check: harvest missing "
                          f"catalog/data/{key}.jsonl — run the repo's ingest "
                          f"(brain/ingest/lean_repo.py)")
                continue
            d_orig = d
            if d and d not in names:
                # same unique-dotted-suffix completion as fc_link (proposers
                # sometimes drop the top-level namespace); ambiguity rejects.
                cands = [n for n in names if n.endswith("." + d)]
                if len(cands) == 1:
                    d = cands[0]
            if not d or d not in names:
                reject(r, f"fold-check: decl not in the {key} harvest: {d}")
                continue
            info = qid_info(qid)
            if info is None or info.get("missing"):
                reject(r, "fold-check: qid missing upstream")
                continue
            if not label_agrees(r, info):
                reject(r, f"fold-check: label mismatch (upstream: {info.get('label')!r})")
                continue
            evidence = {"note": r.get("evidence"), "module": r.get("module"),
                        "file": r.get("file"), "proposer": r.get("proposer"),
                        "skeptic": skeptic, "shard": r.get("_shard"),
                        "decl_as_proposed": d_orig if d != d_orig else None}
            repo_out.setdefault(key, {})[(qid, d)] = {
                "qid": qid, "decl": d, "repo": key, "kind": "mentions",
                "confidence": conf,
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


    # fc links: same merge/retraction semantics as ext anchors — regenerated
    # from all verified proposals, merged with the existing file, minus every
    # (qid, decl) key rejected THIS fold (a refuted join must be withdrawable).
    fc_retract: set[tuple[str, str]] = set()
    _fc_names_for_retract = fc_decl_names()
    for r in rejected:
        if rtype(r) == "fc_link" and r.get("qid") and r.get("decl"):
            fc_retract.add(_completed_retract_key(
                r["qid"], r["decl"], _fc_names_for_retract))
    fcl_path = DATA / "fc_links.jsonl"
    n_fc = 0
    if fc_out or fcl_path.exists():
        fc_merged: dict[tuple[str, str], dict] = {}
        if fcl_path.exists():
            for line in fcl_path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    continue
                if row.get("qid") and row.get("decl"):
                    key = (row["qid"], row["decl"])
                    if key not in fc_retract:
                        fc_merged[key] = row
        fc_merged.update(fc_out)
        n_fc = len(fc_merged)
        meta = {"_meta": {"source": "brain/fold_proposals.py",
                          "inputs": "brain/proposals/fc_links_*.jsonl",
                          "n_rows": n_fc}}
        dump(fcl_path, [meta] + [fc_merged[k] for k in sorted(fc_merged)])

    # repo links (generic frontier-repo agent joins): same merge/retraction
    # semantics as fc links, one catalog/data/<key>_links.jsonl per repo key.
    # Existing keys with no new rows this fold still get retraction applied
    # (a refuted join must be withdrawable), which is why the loop covers
    # every key that has EITHER folded rows or an existing file.
    repo_retract: dict[str, set[tuple[str, str]]] = {}
    for r in rejected:
        key = r.get("repo")
        if rtype(r) == "repo_link" and r.get("qid") and r.get("decl") \
                and isinstance(key, str) and REPO_KEY_RE.match(key) \
                and key in repo_keys:  # never a path from an unvalidated row
            repo_retract.setdefault(key, set()).add(_completed_retract_key(
                r["qid"], r["decl"], frontier_decl_names(key)))
    n_repo_links: dict[str, int] = {}
    # keys with folded rows always write; keys with only retractions rewrite
    # an existing file; untouched keys' files are never rewritten (mtime is
    # build_common's edge pin)
    live_keys = set(repo_out) | {
        k for k in repo_retract if (CATALOG / f"{k}_links.jsonl").exists()}
    for key in sorted(live_keys):
        rl_path = CATALOG / f"{key}_links.jsonl"
        rl_merged: dict[tuple[str, str], dict] = {}
        if rl_path.exists():
            for line in rl_path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    continue
                if row.get("qid") and row.get("decl"):
                    kk = (row["qid"], row["decl"])
                    if kk not in repo_retract.get(key, set()):
                        rl_merged[kk] = row
        rl_merged.update(repo_out.get(key, {}))
        n_repo_links[key] = len(rl_merged)
        meta = {"_meta": {"source": "brain/fold_proposals.py",
                          "inputs": f"brain/proposals/repo_link_{key}_*.jsonl",
                          "repo": key, "kinds": ["mentions"],
                          "n_rows": len(rl_merged)}}
        dump(rl_path, [meta] + [rl_merged[k] for k in sorted(rl_merged)])

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
        | {k[0] for k in xref_out} | {k[0] for k in fc_out} \
        | {k[0] for m in repo_out.values() for k in m}
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

    n_repo_folded = sum(len(m) for m in repo_out.values())
    repo_note = "; ".join(
        f"{k}: {len(repo_out.get(k, {}))} folded ({n_repo_links[k]} total in "
        f"catalog/data/{k}_links.jsonl)" for k in sorted(n_repo_links)) or "none"
    n_pending = sum(1 for v in list(containers_out.values()) + list(discovery_out.values())
                    + list(xref_out.values()) + list(fc_out.values())
                    + [row for m in repo_out.values() for row in m.values()]
                    if (v.get("skeptic") or v["evidence"].get("skeptic")) == "pending")
    print(f"folded: {len(containers_out)} container links, {len(discovery_out)} discovery "
          f"links, {len(xref_out)} ext-anchor links ({n_xref} total in file), "
          f"{len(fc_out)} fc links ({n_fc} total in file), "
          f"{n_repo_folded} repo links [{repo_note}], "
          f"{added_ov} new overrides, {added_ext} universe-extension rows; "
          f"{n_ok} ok-confirmations; {len(rejected)} rejected; "
          f"{len(disputes)} grading disputes (review → grounding_overrides.jsonl); "
          f"{n_pending} rows carry skeptic:pending (capped at medium confidence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
