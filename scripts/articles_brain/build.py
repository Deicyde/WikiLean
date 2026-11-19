"""Offline, deterministic build of the `wikilean.brain.articles/v1` export.

Reads only the pinned inputs under --inputs (see fetch.py for the layout) and writes a
sealed export under --out/<logical_root>/. The same inputs give byte-identical output:
no clock, no network, no host data, every row sorted by its key.

The rows are Brain proposal rows in exactly the shapes `brain/fold_proposals.py` accepts
today — a `discover` row (concept QID → Mathlib declaration, `formalizes`) and a
`repo_link` row (`mentions`, the only kind that channel folds). Everything they cite is a
pointer: store sha, page id, passage key, pair key, record-set seal or overlay version.
No passage text, no note and no Wikipedia prose is copied. The policy is docs/ARTICLES.md
section 13, "The Brain path"; the contract is docs/ARTICLES-BRAIN.md.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from scripts.articles.build_store import STATUS_RULE_D1, location_status

from .canon import dumps, jsonl_bytes, read_json, sha256_bytes, sha256_file, write_bytes
from .fetch import (
    PINS_SCHEMA, SHA_RE, FetchError, check_article, check_overlay, pageids_of, store_identity,
)

SCHEMA = "wikilean.brain.articles/v1"
PRODUCER = {"name": "scripts.articles_brain", "version": "1.0.0"}
PROPOSER = "articles-brain/1"
FILES = ["proposals.jsonl", "rejected.jsonl"]
CLOCK_KEYS = {"fetched_at"}
#: The one namespaced key the rows add. `fold_proposals` copies only the fields it knows
#: into brain/data, so an unknown key rides along and is ignored (docs/ARTICLES-BRAIN.md).
EVIDENCE_KEY = "wikilean_articles"
EVIDENCE_SCHEMA = "wikilean.brain.articles.evidence/1"
MAX_MORE_EVIDENCE = 8

QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")  # brain/fold_proposals.QID_RE
#: wiki/src/articles-overlay.ts validDeclaration, as much of it as a proposal needs.
DECLARATION_RE = re.compile(r"^[^\s.]+(?:\.[^\s.]+)*$")
#: Control, format and line/paragraph separators. DECLARATION_RE already forbids every
#: whitespace character, so what is left to refuse is the invisible ones.
CONTROL_RE = re.compile("[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\ufeff]")
MAX_DECLARATION_CHARS = 240

#: Section 13: "each library maps to one `brain_lib` token". A channel is keyed by the
#: token AND the repository the universe gives it, so a universe cannot move a library
#: onto another library's channel by relabelling it (wiki/src/libraries.ts has the same
#: concern). Mathlib is the one `formalizes` channel; every other library is
#: mentions-only by current policy, through the fold's `repo_link` channel, whose `repo`
#: must be a catalog/data/source_registry.json frontier_sources key with a harvest to
#: check the declaration against.
MATHLIB_LIB = "Mathlib"
MATHLIB_REPO = "https://github.com/leanprover-community/mathlib4"
MENTIONS_CHANNELS = {
    ("TauCeti", "https://github.com/TauCetiProject/TauCeti"): "tauceti",
    ("FormalConjectures", "https://github.com/google-deepmind/formal-conjectures"): "formal_conjectures",
}

#: The `match_kind` a `formalizes` row carries, by the store's relation
#: (scripts/pipeline/vocab.STRONG_RELATIONS: the three a status can rest on).
#:
#: SCHEMA.md rule 1 fuses concept and decl both ways on `match_kind == exact`, so only
#: the store's exactMatch — its identity claim — gets `exact`. The other two keep the
#: store's own token, which no Brain rule reads: rule 2 attaches a concept to a cell on
#: `generalization`/`special_case`, and how wide that rule should be is explicitly
#: Jack's call (SCHEMA.md rule 2), so this producer does not make it for him. See
#: docs/ARTICLES-BRAIN.md, "Left for Jack".
MATCH_KIND = {"exactMatch": "exact", "closeMatch": "closeMatch", "broadMatch": "broadMatch"}
#: A relatedMatch "states something else" (docs/ARTICLES.md): never a location, never a
#: mention.
RELATED_RELATION = "relatedMatch"

ATTRIBUTION = [
    {"work": "WikiLean formalization reader (the articles plane)",
     "url": "https://wikilean.jackmccarthy.org/articles", "license": "CC0-1.0",
     "holder": "WikiLean contributors",
     "use": "the mapping only (which declaration states which passage): page ids, passage "
            "and pair keys, library ids, relations and statuses. No passage text, no note "
            "and no Wikipedia prose is copied."},
    {"work": "Lean declaration names", "url": "https://github.com/leanprover-community/mathlib4",
     "license": "Apache-2.0", "holder": "each library's contributors",
     "use": "declaration names, as the Brain's decl node ids"},
]


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def git_work_tree_holding(path: str) -> str | None:
    """The git work tree `path` would lie in (its nearest ancestor holding `.git`), if any."""
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def load_inputs(inputs: str) -> dict:
    """Every pinned file, with its sha256 re-checked. A mismatch refuses the build."""
    pins_path = os.path.join(inputs, "PINS.json")
    if not os.path.exists(pins_path):
        raise BuildError(f"{pins_path} is missing; run fetch first")
    pins = read_json(pins_path)
    if not isinstance(pins, dict) or pins.get("schema") != PINS_SCHEMA:
        raise BuildError(f"{pins_path}: not a {PINS_SCHEMA} pins file")
    groups = pins.get("inputs")
    if not isinstance(groups, dict):
        raise BuildError(f"{pins_path}: the pins carry no inputs")

    root = os.path.abspath(inputs)

    def check(entry: dict, what: str) -> str:
        rel, sha = entry.get("path"), entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(sha, str) or not SHA_RE.match(sha):
            raise BuildError(f"{what}: the pin names no path and sha256")
        path = os.path.join(inputs, rel)
        # A pins file is data, not trusted configuration: a pin names a file of the
        # input set, never one outside it.
        resolved = os.path.abspath(path)
        if os.path.isabs(rel) or not resolved.startswith(root + os.sep):
            raise BuildError(f"{what}: the pin names {rel}, outside --inputs")
        if not os.path.exists(path):
            raise BuildError(f"{what}: {rel} is missing")
        found = sha256_file(path)
        if found != sha:
            raise BuildError(f"{what}: {rel} has sha256 {found}, pinned {sha}")
        return rel

    loaded: dict = {"pins": pins, "index": {}, "articles": {}, "overlay": {}}
    loaded["current"] = read_json(os.path.join(inputs, check(groups.get("current") or {}, "current")))
    universes = groups.get("universe") or {}
    if len(universes) != 1:
        raise BuildError(f"the pins name {len(universes)} universes; a build has exactly one")
    [(universe_sha, entry)] = universes.items()
    loaded["universe"] = read_json(os.path.join(inputs, check(entry, f"universe {universe_sha}")))
    for group in ("index", "articles", "overlay"):
        for name, entry in sorted((groups.get(group) or {}).items()):
            rel = check(entry, f"{group} {name}")
            loaded[group][name] = read_json(os.path.join(inputs, rel))
    if set(loaded["articles"]) != set(loaded["overlay"]):
        raise BuildError("every article must have an overlay and every overlay an article")
    return loaded


def check_one_build(loaded: dict) -> dict:
    """Refuse inputs that straddle two published builds, or are not the whole build.

    Every check `fetch` makes over the answers is made again here over the pinned
    bytes, because a pins file can be replayed and edited: an edited pins file that
    names only some of the build's pages, or another page's overlay, would otherwise
    build a partial export carrying the whole build's identity.
    """
    try:
        identity = store_identity(loaded["current"])
        for name, article in sorted(loaded["articles"].items()):
            check_article(article, int(name), identity)
        for name, overlay in sorted(loaded["overlay"].items()):
            check_overlay(overlay, int(name))
        index = loaded["current"].get("index") or {}
        shards = index.get("shards")
        if not isinstance(shards, list) or not shards:
            raise BuildError("current.json: the build lists no index shard")
        if set(loaded["index"]) != {str(shard) for shard in shards}:
            raise BuildError(f"the pins name index shards {sorted(loaded['index'])}, "
                             f"the build's are {sorted(str(s) for s in shards)}")
        pageids: list[int] = []
        for name in sorted(loaded["index"]):
            pageids += pageids_of(loaded["index"][name], f"index/{name}.json")
        pages = {str(pageid) for pageid in pageids}
        entries = index.get("entries")
        if isinstance(entries, int) and entries != len(pages):
            raise BuildError(f"the index shards hold {len(pages)} pages, "
                             f"current.json says {entries}")
        if set(loaded["articles"]) != pages:
            missing = sorted(pages - set(loaded["articles"]))
            extra = sorted(set(loaded["articles"]) - pages)
            raise BuildError(f"the pinned articles are not the build's pages: "
                             f"{len(missing)} missing, {len(extra)} not in the index "
                             f"({', '.join((missing + extra)[:5])})")
    except (FetchError, ValueError) as error:
        raise BuildError(str(error)) from None
    served = loaded["universe"].get("sha256")
    if served != identity["universe_sha256"]:
        raise BuildError(f"the pinned universe is {served}, the build's is {identity['universe_sha256']}")
    return identity


# ---------------------------------------------------------------- the merge

def valid_declaration(name: object) -> bool:
    return (isinstance(name, str) and 1 <= len(name) <= MAX_DECLARATION_CHARS
            and not CONTROL_RE.search(name) and bool(DECLARATION_RE.match(name)))


def status_rule_of(article: dict) -> int:
    """The derivation the article's set was sealed under (docs/ARTICLES.md section 3)."""
    rule = (article.get("seal") or {}).get("status_rule")
    return rule if isinstance(rule, int) else STATUS_RULE_D1


def store_status(passage: dict, loc: dict, status_rule: int) -> str | None:
    """What the STORE says this one location gives this passage.

    `scripts/articles/build_store.location_status`, imported rather than ported: the
    whole point is to reuse the plane's judgment, so there is nothing here to drift.
    """
    try:
        return location_status(str(passage.get("kind") or ""), loc, status_rule)
    except (KeyError, TypeError, ValueError):
        return None


def community_location(tag: dict, commit: str) -> dict:
    """wiki/src/articles-overlay.ts communityLocation: the card a community tag shows.

    `proof_status` is `statement_only` there, deliberately — the site never mints a
    formalization claim on one editor's word — which is why a community tag can never
    reach the `formalizes` channel here either.
    """
    return {
        "library": tag.get("library"), "repo": tag.get("repository"), "commit": commit,
        "declaration": tag.get("declaration"), "module": "", "path": "", "line": 0,
        "relation": tag.get("relation"), "proof_status": "statement_only", "rank": 0,
        "confidence": "community", "challenge": "none",
        "location_key": tag.get("pair_key"), "pair_key": tag.get("pair_key"),
        "note": tag.get("note"), "statement": False,
    }


def merge_passage(passage: dict, by_pair: dict, by_passage: dict) -> list[dict]:
    """The location half of wiki/src/articles-overlay.ts mergeOverlay, ported.

    Returns `{loc, origin, state, tag, base_relation}` per merged location, in the
    sealed order then the community-only tags by pair key. A community row at a sealed
    location's `pair_key` IS that location: a removal withdraws it and a live row may
    change its relation. `base_relation` is the sealed relation when a live row changed
    the location (the merge's `baseRelation`), so a row never cites the record set for
    a relation the record set does not state.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for loc in passage.get("locations") or []:
        key = loc.get("pair_key") if isinstance(loc.get("pair_key"), str) else None
        tag = by_pair.get(key) if key else None
        if key:
            seen.add(key)
        if tag is None:
            merged.append({"loc": loc, "origin": "pipeline", "state": "live", "tag": None,
                           "base_relation": None})
            continue
        live = tag.get("state") == "live"
        confirms = live and tag.get("relation") == loc.get("relation") \
            and (tag.get("note") or None) == (loc.get("note") or None)
        changed = live and not confirms
        merged.append({
            "loc": {**loc, "relation": tag.get("relation"), "note": tag.get("note")} if changed else loc,
            "origin": "pipeline", "state": tag.get("state"), "tag": tag,
            "base_relation": loc.get("relation") if changed else None,
        })
    key = passage.get("passage_key") if isinstance(passage.get("passage_key"), str) else ""
    for tag in sorted(by_passage.get(key, []), key=lambda t: str(t.get("pair_key"))):
        if tag.get("pair_key") in seen:
            continue
        commit = next((loc.get("commit") for loc in passage.get("locations") or []
                       if loc.get("library") == tag.get("library")), "")
        merged.append({"loc": community_location(tag, commit or ""), "origin": "community",
                       "state": tag.get("state"), "tag": tag, "base_relation": None})
    return merged


# ---------------------------------------------------------------- rows

def pointer(store: str, article: dict, passage: dict, entry: dict, library: dict | None,
            overlay_version: int | None, include_author: bool,
            status_rule: int = STATUS_RULE_D1) -> dict:
    """One row's evidence, by pointer only."""
    loc, tag = entry["loc"], entry["tag"]
    found = {
        "schema": EVIDENCE_SCHEMA,
        "store": store,
        "pageid": article["pageid"],
        "passage_key": passage.get("passage_key"),
        "pair_key": loc.get("pair_key"),
        "location_key": loc.get("location_key"),
        "prominence": passage.get("prominence"),
        "passage_kind": passage.get("kind"),
        "sealed_status": passage.get("status"),
        "status_rule": status_rule,
        "location_status": store_status(passage, loc, status_rule),
        "relation": loc.get("relation"),
        "base_relation": entry.get("base_relation"),
        "proof_status": loc.get("proof_status"),
        "library": loc.get("library"),
        "brain_lib": (library or {}).get("brain_lib"),
        "repo": loc.get("repo"),
        "commit": loc.get("commit"),
        "origin": entry["origin"],
        "actor_type": actor_type(entry),
        "source": "overlay" if entry["origin"] == "community" else "record_set",
        "record_set": article.get("record_set"),
        "overlay_version": tag.get("version") if isinstance(tag, dict) else None,
        "overlay_max_version": overlay_version,
    }
    if tag is not None:
        found["overlay_state"] = tag.get("state")
        found["overlay_via"] = tag.get("via")
        if include_author:
            author = tag.get("added_by")
            found["added_by"] = author.get("id") if isinstance(author, dict) else author
    return {k: v for k, v in found.items() if v is not None}


def actor_type(entry: dict) -> str:
    """Who wrote the finding: a person, or an AI.

    A sealed record-set location is an AI locate run's (challenged and audited; the
    store records both). An overlay row says so itself: `human` or `agent`.
    """
    tag = entry["tag"]
    if entry["origin"] == "community" or (tag is not None and tag.get("state") == "live"):
        return "human" if (tag or {}).get("actor_type") == "human" else "ai"
    return "ai"


def evidence_text(found: dict) -> str:
    """A one-line pointer. Never prose: every part of it is an identifier."""
    parts = [
        f"store {str(found['store'])[:12]}",
        f"page {found['pageid']}",
        f"passage {str(found.get('passage_key'))[:12]}",
        f"pair {str(found.get('pair_key'))[:12]}",
        f"library {found.get('library')} ({found.get('brain_lib')})",
        f"relation {found.get('relation')}",
        f"proof_status {found.get('proof_status')}",
        f"sealed status {found.get('sealed_status')}",
    ]
    if found["source"] == "overlay":
        parts.append(f"overlay tag version {found.get('overlay_version')}")
    else:
        parts.append(f"record set {str(found.get('record_set'))[:12]}")
    parts.append(f"actor {found['actor_type']}")
    return "WikiLean articles plane: " + ", ".join(parts)


def row_key(row: dict) -> tuple:
    return (row.get("action") or "", row.get("repo") or "", row["qid"], row["decl"])


def confidence_of(actor: str) -> str:
    """The claim's own confidence. The fold caps a row with no skeptic verdict at
    `medium` whatever this says, so `high` is a statement about the evidence, not a
    way round the cap (docs/ARTICLES-BRAIN.md)."""
    return "high" if actor == "human" else "medium"


def discover_row(article: dict, found: dict, loc: dict) -> dict:
    """A `formalizes` proposal: concept QID → Mathlib declaration.

    No `action` and a `decl`, so brain/fold_proposals.main's `rtype` reads it as
    `discover`, whose branch mints `{"src": qid, "dst": "decl:Mathlib:<decl>",
    "kind": "formalizes"}`.
    """
    row = {
        "qid": article["wikidata"],
        "qid_label": article["title"],
        "decl": loc["declaration"],
        "match_kind": MATCH_KIND[loc["relation"]],
        "confidence": confidence_of(found["actor_type"]),
        "proposer": PROPOSER,
        "evidence": evidence_text(found),
        EVIDENCE_KEY: found,
    }
    if isinstance(loc.get("module"), str) and loc["module"]:
        row["module"] = loc["module"]
    return row


def repo_link_row(article: dict, found: dict, loc: dict, repo_key: str) -> dict:
    """A `mentions` proposal for a library other than Mathlib.

    `repo_link` is the fold's generic frontier-repo channel and folds `mentions` only —
    "AI joins never mint identity claims", its moderation contract — which is exactly
    section 13's "Non-Mathlib libraries stay mentions-only by current policy".
    """
    row = {
        "action": "repo_link",
        "repo": repo_key,
        "qid": article["wikidata"],
        "qid_label": article["title"],
        "decl": loc["declaration"],
        "kind": "mentions",
        "confidence": confidence_of(found["actor_type"]),
        "proposer": PROPOSER,
        "evidence": evidence_text(found),
        EVIDENCE_KEY: found,
    }
    if isinstance(loc.get("module"), str) and loc["module"]:
        row["module"] = loc["module"]
    return row


# ---------------------------------------------------------------- the rules

def classify(article: dict, passage: dict, entry: dict, libraries: dict,
             status_rule: int) -> tuple[str | None, str]:
    """`(channel, reason)`: the channel this candidate folds through, else why not.

    The checks are in a fixed order, so the rejection counts read as a funnel.
    """
    loc = entry["loc"]
    if passage.get("prominence") != "subject":
        return None, "not_subject_passage"
    qid = article.get("wikidata")
    if qid is None:
        return None, "article_has_no_qid"
    if not (isinstance(qid, str) and QID_RE.match(qid)):
        return None, "qid_not_canonical"
    if not isinstance(article.get("title"), str) or not article["title"].strip():
        return None, "article_has_no_title"
    if entry["state"] != "live":
        return None, "withdrawn_by_the_overlay"
    if loc.get("relation") == RELATED_RELATION:
        return None, "relation_related_match"
    if not valid_declaration(loc.get("declaration")):
        return None, "declaration_not_a_lean_name"
    library = libraries.get(loc.get("library"))
    if library is None:
        return None, "library_not_in_the_universe"
    if loc.get("repo") != library.get("repo"):
        return None, "library_repo_mismatch"
    token = library.get("brain_lib")
    if token == MATHLIB_LIB and library.get("repo") == MATHLIB_REPO:
        if entry["origin"] == "community":
            # The fold has no Mathlib mentions channel, and a community tag may never
            # mint an identity claim (docs/ARTICLES-BRAIN.md, "Left for Jack").
            return None, "mathlib_community_tag_has_no_mentions_channel"
        if entry.get("base_relation") not in (None, loc.get("relation")):
            # A live community row at a sealed location's pair key may relabel it, and
            # the relation IS the identity claim: SCHEMA.md rule 1 fuses concept and
            # decl on `match_kind == exact`. One editor's (or one agent's) relabelling
            # of a closeMatch, a broadMatch or a relatedMatch must never mint or
            # strengthen a formalizes edge, which is what "the site never mints a
            # formalization claim on one editor's word" means here. A note-only change
            # leaves the row: base_relation then equals the merged relation.
            return None, "community_relation_change_on_a_mathlib_location"
        if passage.get("outcome") != "located":
            return None, "passage_not_located"
        if passage.get("status") != "formalized":
            return None, "status_not_formalized"
        # The store's own per-location rule, under the set's own status rule: this
        # location, on its own, gives the passage `formalized`. A part of a coverage
        # bundle gives `partial` and stops here, as it should — no single declaration
        # states a passage the bundle covers.
        if store_status(passage, loc, status_rule) != "formalized":
            return None, "location_does_not_formalize"
        if loc.get("relation") not in MATCH_KIND:
            return None, "relation_not_strong"  # unreachable today: only the three
        return "formalizes", ""
    key = MENTIONS_CHANNELS.get((token, library.get("repo")))
    if key is None:
        return None, "no_brain_channel_for_library"
    return key, ""


def rejection(kind: str, reason: str, found: dict) -> dict:
    row = {"kind": kind, "reason": reason, **{k: v for k, v in found.items() if k != "schema"}}
    row["id"] = f"{kind}:{sha256_bytes(dumps(row).encode('utf-8'))[:20]}"
    return row


def candidates(loaded: dict, include_author: bool) -> tuple[list[dict], list[dict], dict]:
    rows: dict[tuple, dict] = {}
    rejected: list[dict] = []
    counts = Counter()
    store = (loaded["pins"]["inputs"].get("current") or {}).get("sha256")
    libraries = {lib["id"]: lib for lib in (loaded["universe"].get("libraries") or [])
                 if isinstance(lib, dict) and isinstance(lib.get("id"), str)}
    for name in sorted(loaded["articles"], key=int):
        article = loaded["articles"][name]
        overlay = loaded["overlay"][name]
        tags = [t for t in (overlay.get("tags") or []) if isinstance(t, dict)]
        versions = [t["version"] for t in tags if isinstance(t.get("version"), int)]
        overlay_version = max(versions) if versions else 0
        by_pair = {t["pair_key"]: t for t in tags if isinstance(t.get("pair_key"), str)}
        by_passage: dict[str, list[dict]] = {}
        for tag in tags:
            by_passage.setdefault(str(tag.get("passage_key")), []).append(tag)
        counts["articles"] += 1
        counts["overlay_tags"] += len(tags)
        counts["overlay_tags_live"] += sum(1 for t in tags if t.get("state") == "live")
        counts["overlay_bundles"] += len(overlay.get("bundles") or [])
        status_rule = status_rule_of(article)

        passage_keys = set()
        for passage in article.get("passages") or []:
            counts["passages"] += 1
            if passage.get("prominence") == "subject":
                counts["subject_passages"] += 1
            key = passage.get("passage_key")
            if isinstance(key, str):
                passage_keys.add(key)
            for entry in merge_passage(passage, by_pair, by_passage):
                counts["candidates"] += 1
                library = libraries.get((entry["loc"] or {}).get("library"))
                found = pointer(store, article, passage, entry, library, overlay_version,
                                include_author, status_rule)
                channel, reason = classify(article, passage, entry, libraries, status_rule)
                if channel is None:
                    rejected.append(rejection("location", reason, found))
                    continue
                loc = entry["loc"]
                row = discover_row(article, found, loc) if channel == "formalizes" \
                    else repo_link_row(article, found, loc, channel)
                key = row_key(row)
                first = rows.get(key)
                if first is None:
                    rows[key] = row
                    continue
                # The same join, found again on another passage of the article: one row,
                # the first pointer, and the rest listed beside it.
                block = first[EVIDENCE_KEY]
                block["evidence_count"] = block.get("evidence_count", 1) + 1
                more = block.setdefault("more_evidence", [])
                if len(more) < MAX_MORE_EVIDENCE:
                    more.append({k: found[k] for k in ("pageid", "passage_key", "pair_key")
                                 if k in found})
        for tag in sorted(tags, key=lambda t: str(t.get("pair_key"))):
            if str(tag.get("passage_key")) not in passage_keys:
                entry = {"loc": community_location(tag, ""), "origin": "community",
                         "state": tag.get("state"), "tag": tag, "base_relation": None}
                found = pointer(store, article, {"passage_key": tag.get("passage_key")},
                                entry, libraries.get(tag.get("library")), overlay_version,
                                include_author, status_rule)
                rejected.append(rejection("tag", "tag_passage_not_in_the_article", found))
    return list(rows.values()), rejected, dict(counts)


# ---------------------------------------------------------------- the export

def dedupe_rejected(rejected: list[dict]) -> list[dict]:
    """Identical rejections are one row with `count` (scripts/tauceti_plans convention)."""
    merged: dict[str, dict] = {}
    for row in rejected:
        seen = merged.get(row["id"])
        if seen is None:
            merged[row["id"]] = {**row, "count": 1}
        else:
            seen["count"] += 1
    return list(merged.values())


def strip_clocks(obj):
    if isinstance(obj, dict):
        return {k: strip_clocks(v) for k, v in obj.items() if k not in CLOCK_KEYS}
    if isinstance(obj, list):
        return [strip_clocks(v) for v in obj]
    return obj


def logical_root(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "logical_root"}
    return sha256_bytes(dumps(strip_clocks(body)).encode("utf-8"))


def make_manifest(loaded: dict, identity: dict, data: dict, counts: dict,
                  include_author: bool) -> dict:
    pins = loaded["pins"]
    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "policy": "docs/ARTICLES.md section 13, The Brain path",
        "fold": "brain/fold_proposals.py",
        "site": pins.get("site"),
        "store": identity,
        "inputs": pins.get("inputs"),
        "options": {"include_author_identity": include_author},
        "channels": {
            "formalizes": {"brain_lib": MATHLIB_LIB, "repo": MATHLIB_REPO,
                           "row": "discover", "kind": "formalizes", "match_kind": "exact"},
            "mentions": {f"{token} ({repo})": {"row": "repo_link", "repo": key, "kind": "mentions"}
                         for (token, repo), key in sorted(MENTIONS_CHANNELS.items())},
        },
        "files": {name: {"rows": blob.count(b"\n"), "bytes": len(blob),
                         "sha256": sha256_bytes(blob)}
                  for name, blob in sorted(data.items())},
        "counts": counts,
        "clock_fields": sorted(CLOCK_KEYS),
        "attribution": ATTRIBUTION,
    }
    manifest["logical_root"] = logical_root(manifest)
    return manifest


def build(inputs: str, out: str, include_author: bool = False) -> tuple[str, dict]:
    """Build the sealed export into `<out>/<logical_root>/`. Returns (path, manifest)."""
    tree_root = git_work_tree_holding(out)
    if tree_root is not None:
        raise BuildError(f"--out {out} lies inside the git work tree {tree_root}; "
                         "an export is never committed — write it elsewhere")
    loaded = load_inputs(inputs)
    identity = check_one_build(loaded)
    rows, rejected, counts = candidates(loaded, include_author)

    by_kind = Counter()
    for row in rows:
        by_kind[row.get("kind") or "formalizes"] += 1
    by_repo = Counter(row["repo"] for row in rows if row.get("action") == "repo_link")
    rejected = dedupe_rejected(rejected)
    by_reason = Counter()
    for row in rejected:
        by_reason[row["reason"]] += row["count"]
    counts = {
        **counts,
        "rows": len(rows),
        "rows_by_kind": dict(sorted(by_kind.items())),
        "rows_by_repo": dict(sorted(by_repo.items())),
        "rejections": sum(by_reason.values()),
        "rejections_by_reason": dict(sorted(by_reason.items())),
    }

    data = {
        "proposals.jsonl": jsonl_bytes(rows, row_key),
        "rejected.jsonl": jsonl_bytes(rejected, lambda r: r["id"]),
    }
    manifest = make_manifest(loaded, identity, data, counts, include_author)
    target = os.path.join(out, manifest["logical_root"])
    os.makedirs(target, exist_ok=True)
    for name, blob in data.items():
        write_bytes(os.path.join(target, name), blob)
    write_bytes(os.path.join(target, "manifest.json"),
                (dumps(manifest) + "\n").encode("utf-8"))
    return target, manifest
