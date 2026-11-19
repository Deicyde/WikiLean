"""A synthetic articles plane, for the tests.

Nothing here comes from the live site: the articles are invented, the passages carry no
Wikipedia text (the real store's quotes are CC BY-SA and this repository is public), and
the declaration names are made up. The shapes are the real ones — `wikilean.articles.*`
view models and the overlay route's rows — so the tests exercise the same code paths the
real run does.
"""

from __future__ import annotations

import json

SITE = "https://articles.invalid"
UNIVERSE_SHA = "a" * 64
SEAL_ONE = "b" * 64
SEAL_TWO = "c" * 64
OTHER_UNIVERSE_SHA = "d" * 64

MATHLIB = "https://github.com/leanprover-community/mathlib4"
TAUCETI = "https://github.com/TauCetiProject/TauCeti"
LEAN4 = "https://github.com/leanprover/lean4"
WIDGETS = "https://github.com/FormalFrontier/WidgetTheory"

LIBRARIES = [
    {"id": "ff-widgets", "brain_lib": "WidgetTheory", "repo": WIDGETS,
     "commit": "1" * 40, "licence": "Apache-2.0", "roots": ["WidgetTheory"],
     "toolchain": "leanprover/lean4:v4.24.0"},
    {"id": "lean-core", "brain_lib": "LeanCore", "repo": LEAN4,
     "commit": "2" * 40, "licence": "Apache-2.0", "roots": ["Init"],
     "toolchain": "leanprover/lean4:v4.24.0"},
    {"id": "mathlib", "brain_lib": "Mathlib", "repo": MATHLIB,
     "commit": "3" * 40, "licence": "Apache-2.0", "roots": ["Mathlib"],
     "toolchain": "leanprover/lean4:v4.24.0"},
    {"id": "tauceti", "brain_lib": "TauCeti", "repo": TAUCETI,
     "commit": "4" * 40, "licence": "Apache-2.0", "roots": ["TauCeti"],
     "toolchain": "leanprover/lean4:v4.24.0"},
]


def location(library: str, repo: str, commit: str, declaration: str, relation: str,
             proof_status: str, key: str, rank: int = 1, module: str = "",
             statement: bool = False) -> dict:
    return {"library": library, "repo": repo, "commit": commit, "declaration": declaration,
            "module": module, "path": "Src/File.lean", "line": 10, "end_line": 12,
            "relation": relation, "proof_status": proof_status, "rank": rank,
            "confidence": "high", "challenge": "survived", "statement": statement,
            "location_key": key, "pair_key": key, "note": None}


def passage(item: str, prominence: str, key: str, status: str | None, locations: list[dict],
            basis: str | None = None, outcome: str = "located", kind: str = "theorem",
            coverage: dict | None = None) -> dict:
    absence = ({"universe": "u-fixture", "universe_sha256": UNIVERSE_SHA, "libraries": [],
                "receipt": {"sha256": "0" * 64, "queries": 1, "candidates": 0},
                "challenge": "survived"} if outcome == "absent" else None)
    return {"id": f"enwiki::{item}", "item": item, "kind": kind, "section": [],
            "prominence": prominence, "quote": {"exact": ""}, "quote_capped": False,
            "passage_ends_in_suffix": False, "subject_uri": "https://example.invalid/",
            "outcome": outcome, "status": status, "basis": basis, "coverage": coverage,
            "absence": absence, "unresolved": None, "passage_key": key,
            "locations": locations}


def article(pageid: int, title: str, wikidata: str | None, passages: list[dict],
            seal: str = SEAL_ONE, universe_sha: str = UNIVERSE_SHA) -> dict:
    return {
        "schema": "wikilean.articles.article/1", "pageid": pageid, "title": title,
        "revision": 1, "source_id": f"enwiki-{pageid}-1", "wikidata": wikidata,
        "assessment": {"importance": "high"}, "campaign": "fixture",
        "universe": {"id": "u-fixture", "sha256": universe_sha, "frozen": "2026-10-17",
                     "libraries": len(LIBRARIES)},
        "record_set": seal, "records_sha256": "e" * 64,
        "licence": {"text": "CC-BY-SA-4.0", "mapping": "CC0-1.0"},
        "seal": {"set_id": "fixture", "schema": "cf-locate-recordset/2", "status_rule": 3,
                 "created": "2026-11-01T00:00:00Z", "skill_commit": "f" * 40},
        "summary": {"passages": len(passages)}, "passages": passages,
    }


def tag(pair_key: str, passage_key: str, library: str, repository: str, declaration: str,
        relation: str, state: str = "live", actor_type: str = "human",
        version: int = 1, note: str | None = None) -> dict:
    return {"pair_key": pair_key, "passage_key": passage_key, "library": library,
            "repository": repository, "declaration": declaration, "relation": relation,
            "note": note, "state": state, "base_relation": None, "actor_type": actor_type,
            "via": "web", "model": None, "run_id": None, "campaign": None, "revision": 1,
            "version": version, "created_at": "2026-11-10T00:00:00Z",
            "updated_at": "2026-11-10T00:00:00Z",
            "removed_at": "2026-11-11T00:00:00Z" if state == "removed" else None,
            "removed_reason": "wrong declaration" if state == "removed" else None,
            "added_by": {"id": "user-1", "name": "A Person", "owner_id": None,
                         "owner_name": None},
            "removed_by": "user-1" if state == "removed" else None}


# ---- the three articles ----------------------------------------------------
# 101: a subject passage the pipeline formalized in Mathlib, with a TauCeti location
#      beside it and a Lean-core one with no Brain channel; and a supporting passage.
# 102: the same finding on an article with no Wikidata item.
# 103: a subject passage three community tags touch, and a sealed Mathlib location a
#      community removal withdraws.

P101A = "1" * 64
P101B = "2" * 64
P101G = "7" * 64
P102C = "3" * 64
P103D = "4" * 64
P103E = "5" * 64
P103F = "6" * 64
K_MATHLIB_ONE = "a1" + "0" * 62
K_TAUCETI_ONE = "a2" + "0" * 62
K_LEANCORE = "a3" + "0" * 62
K_SUPPORTING = "a4" + "0" * 62
K_MATHLIB_BROAD = "a7" + "0" * 62
K_MATHLIB_PART = "a8" + "0" * 62
K_MATHLIB_CLOSE = "a9" + "0" * 62
K_NO_QID = "a5" + "0" * 62
K_MATHLIB_TWO = "a6" + "0" * 62
K_TAG_LIVE = "b1" + "0" * 62
K_TAG_REMOVED = "b2" + "0" * 62
K_TAG_MATHLIB = "b3" + "0" * 62
K_TAG_ORPHAN = "b4" + "0" * 62
K_TAG_UNCHECKED = "b5" + "0" * 62


def articles() -> dict[int, dict]:
    a101 = article(101, "Widget lemma", "Q101", [
        passage("In-text #0001", "subject", P101A, "formalized", [
            location("mathlib", MATHLIB, "3" * 40, "Widget.lemma_one", "exactMatch",
                     "proved", K_MATHLIB_ONE, 1, "Mathlib.Widget.Basic"),
            location("tauceti", TAUCETI, "4" * 40, "TauCeti.Widget.one", "closeMatch",
                     "statement_only", K_TAUCETI_ONE, 2, "TauCeti.Widget"),
            location("lean-core", LEAN4, "2" * 40, "Widget.core", "broadMatch",
                     "proved", K_LEANCORE, 3, "Init.Widget"),
            # A part of the passage: the store says `partial`, so it formalizes nothing.
            location("mathlib", MATHLIB, "3" * 40, "Widget.part", "hasPart",
                     "proved", K_MATHLIB_PART, 4, "Mathlib.Widget.Basic"),
            # A second Mathlib declaration that does state it: formalizes is
            # many-to-many by construction (brain/SCHEMA.md).
            location("mathlib", MATHLIB, "3" * 40, "Widget.close", "closeMatch",
                     "proved", K_MATHLIB_CLOSE, 5, "Mathlib.Widget.Basic"),
        ], basis=K_MATHLIB_ONE),
        # A subject passage the store counts as formalized on a broadMatch (status rule 3
        # formalizes a broadMatch to data): a formalizes row that is not an identity claim.
        passage("In-text #0007", "subject", P101G, "formalized", [
            location("mathlib", MATHLIB, "3" * 40, "Widget.general", "broadMatch",
                     "definition", K_MATHLIB_BROAD, 1, "Mathlib.Widget.General"),
        ], basis=K_MATHLIB_BROAD, kind="definition"),
        passage("In-text #0002", "supporting", P101B, "formalized", [
            location("mathlib", MATHLIB, "3" * 40, "Widget.helper", "exactMatch",
                     "proved", K_SUPPORTING, 1, "Mathlib.Widget.Basic"),
        ], basis=K_SUPPORTING),
    ])
    a102 = article(102, "Widget with no item", None, [
        passage("In-text #0003", "subject", P102C, "formalized", [
            location("mathlib", MATHLIB, "3" * 40, "Widget.lemma_three", "exactMatch",
                     "proved", K_NO_QID, 1, "Mathlib.Widget.Basic"),
        ], basis=K_NO_QID),
    ], seal=SEAL_TWO)
    a103 = article(103, "Tagged widget", "Q103", [
        passage("In-text #0004", "subject", P103D, "not_formalized", [], outcome="absent"),
        passage("In-text #0005", "subject", P103E, "formalized", [
            location("mathlib", MATHLIB, "3" * 40, "Widget.lemma_two", "exactMatch",
                     "proved", K_MATHLIB_TWO, 1, "Mathlib.Widget.Two"),
        ], basis=K_MATHLIB_TWO),
        # Phase 1 (docs/ARTICLES.md section 9a): a published cut nothing has searched.
        passage("In-text #0006", "subject", P103F, None, [], outcome="unchecked"),
    ], seal=SEAL_TWO)
    return {101: a101, 102: a102, 103: a103}


def overlays() -> dict[int, dict]:
    return {
        101: {"ok": True, "pageid": 101, "tags": [], "bundles": []},
        102: {"ok": True, "pageid": 102, "tags": [], "bundles": []},
        103: {"ok": True, "pageid": 103, "bundles": [], "tags": [
            tag(K_TAG_LIVE, P103D, "tauceti", TAUCETI, "TauCeti.Widget.tagged",
                "closeMatch", version=3),
            tag(K_TAG_REMOVED, P103D, "tauceti", TAUCETI, "TauCeti.Widget.withdrawn",
                "exactMatch", state="removed", version=2),
            tag(K_TAG_MATHLIB, P103D, "mathlib", MATHLIB, "Widget.community",
                "exactMatch", actor_type="agent", version=4),
            # A community removal of the pipeline's own location: same pair key.
            tag(K_MATHLIB_TWO, P103E, "mathlib", MATHLIB, "Widget.lemma_two",
                "exactMatch", state="removed", version=5),
            # A tag on a statement no library has been searched for.
            tag(K_TAG_UNCHECKED, P103F, "tauceti", TAUCETI, "TauCeti.Widget.unchecked",
                "exactMatch", actor_type="agent", version=7),
            # A tag whose passage is not in this article's view model.
            tag(K_TAG_ORPHAN, "9" * 64, "tauceti", TAUCETI, "TauCeti.Widget.orphan",
                "exactMatch", version=6),
        ]},
    }


def universe() -> dict:
    return {"schema": "wikilean.articles.universe/1", "sha256": UNIVERSE_SHA,
            "id": "u-fixture", "frozen": "2026-10-17", "libraries": LIBRARIES,
            "excluded": []}


def current() -> dict:
    return {
        "schema": "wikilean.articles.current/1", "builder": "fixture",
        "built_at": "2026-11-01T00:00:00Z", "campaign": "fixture",
        "universe": {"id": "u-fixture", "sha256": UNIVERSE_SHA, "frozen": "2026-10-17",
                     "libraries": len(LIBRARIES)},
        "record_set": {"seal_sha256": SEAL_ONE, "set_id": "fixture-a",
                       "schema": "cf-locate-recordset/2", "status_rule": 3,
                       "created": "2026-11-01T00:00:00Z", "byte_budget": 0},
        "record_sets": [
            {"seal_sha256": SEAL_ONE, "set_id": "fixture-a", "schema": "cf-locate-recordset/2",
             "status_rule": 3, "created": "2026-11-01T00:00:00Z", "byte_budget": 0},
            {"seal_sha256": SEAL_TWO, "set_id": "fixture-b", "schema": "cf-locate-recordset/2",
             "status_rule": 3, "created": "2026-11-01T00:00:00Z", "byte_budget": 0},
        ],
        "index": {"entries": 3, "hex": 1, "shards": ["0"]},
        "counts": {"articles": 3},
    }


def index_shard() -> dict:
    return {"schema": "wikilean.articles.index/1", "shard": "0",
            "fields": ["title", "pageid", "revision", "passages", "formalized",
                       "statement_only", "partial", "not_formalized", "unresolved",
                       "importance", "libraries"],
            "articles": [[a["title"], a["pageid"], 1, len(a["passages"]), 0, 0, 0, 0, 0,
                          "high", []] for a in articles().values()]}


def body(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def site_bodies() -> dict[str, bytes]:
    """path -> the answer's bytes, for every route `fetch` reads."""
    pages = {"/api/articles/current": body(current()),
             "/api/articles/index/0": body(index_shard()),
             f"/api/articles/universe/{UNIVERSE_SHA}": body(universe())}
    for pageid, value in articles().items():
        pages[f"/api/articles/article/{pageid}"] = body(value)
    for pageid, value in overlays().items():
        pages[f"/api/articles/overlay/{pageid}"] = body(value)
    return pages


class FakeOpener:
    """The network, replaced. `script` queues extra answers for one path."""

    def __init__(self, pages: dict[str, bytes] | None = None,
                 script: dict[str, list[tuple[int, dict, bytes]]] | None = None):
        self.pages = dict(pages if pages is not None else site_bodies())
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        path = url[len(SITE):] if url.startswith(SITE) else url
        self.calls.append(path)
        queued = self.script.get(path)
        if queued:
            return queued.pop(0)
        page = self.pages.get(path)
        if page is None:
            return 404, {}, b'{"ok":false}'
        return 200, {}, page
