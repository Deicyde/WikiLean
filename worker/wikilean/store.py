"""Reading the published store, and choosing what to work on.

Everything here is a pure function over JSON the public routes already serve —
``/api/articles/current``, ``/api/articles/index/<shard>``,
``/api/articles/article/<pageid>`` and ``/api/articles/overlay/<pageid>``. No
HTTP, no model, no writes, so the choice of work is testable on a fixture and
is the same on every machine given the same data.

**Which work, and why that work.** docs/START-CONTRIBUTING.md names the two
judgements the blind audit measured weakest, and they are the two units:

* a link the refuting pass **downgraded** — 49.8% against a 68% bar;
* a passage marked **not formalized** whose absence that pass confirmed —
  59.6% against an 85% bar.

Those are the ``review`` unit. A formal passage the refuting pass never judged
and that has no location at all is nobody's claim yet, so proposing one is a
different act: that is the ``tag`` unit. The two candidate sets are disjoint by
construction, so a round never does the second when the first has work.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

# articles-overlay.ts TAG_RELATIONS, verbatim.
RELATIONS: tuple[str, ...] = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch")

# build_store.py CHALLENGES. "downgraded" is the refuting pass having changed
# the record; "survived" is it having tried and failed to.
CHALLENGE_DOWNGRADED = "downgraded"
CHALLENGE_SURVIVED = "survived"

# vocab.py KINDS, with the two that state mathematics first.
FORMAL_KINDS: tuple[str, ...] = ("theorem", "conjecture", "definition", "construction", "notation")
PROMINENCE_ORDER = {"subject": 0, "central": 1, "supporting": 2}

MAX_NOTE_CHARS = 500
MAX_DECLARATION_CHARS = 240


def pair_key(passage_key: str, repository: str, declaration: str) -> str:
    """build_store.py's key, which articles-overlay.ts recomputes server side.

    The CLI derives it only to look a row up in the overlay it already read;
    every write still names the passage and declaration and lets the server
    compute the key it stores.
    """
    return hashlib.sha256(f"{passage_key}\n{repository}\n{declaration}".encode("utf-8")).hexdigest()


def valid_declaration(name: str) -> bool:
    """articles-overlay.ts validDeclaration, in Python.

    The server's rule is: at most 240 characters, no character in Unicode's C
    (control, format, surrogate, private use, unassigned), Zl or Zp classes, no
    whitespace anywhere, and one or more dot-separated non-empty components.
    Matching it here means a refusal is ours, before a request exists, rather
    than a 400 after one.
    """
    if not name or len(name) > MAX_DECLARATION_CHARS:
        return False
    for ch in name:
        if ch.isspace() or unicodedata.category(ch) in ("Zl", "Zp") or unicodedata.category(ch)[0] == "C":
            return False
    return all(part for part in name.split("."))


def valid_relation(relation: str) -> bool:
    return relation in RELATIONS


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ---- the overlay ------------------------------------------------------------


@dataclass(frozen=True)
class Overlay:
    """One article's community rows, by pair key."""

    rows: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def of(cls, tags: Iterable[dict]) -> Overlay:
        return cls({str(tag.get("pair_key")): tag for tag in tags if isinstance(tag, dict) and tag.get("pair_key")})

    def version(self, key: str) -> int:
        """The row's version, or 0 — what ``base_version`` must say."""
        value = self.rows.get(key, {}).get("version")
        return value if isinstance(value, int) and value >= 0 else 0

    def row(self, key: str) -> dict | None:
        return self.rows.get(key)

    def written_by_a_person(self, key: str) -> bool:
        """A live row a human wrote: an agent may not touch it (403), so the
        CLI does not offer it as work rather than earning a refusal."""
        row = self.rows.get(key)
        return bool(row and row.get("actor_type") == "human" and row.get("state") == "live")

    def touched(self, key: str) -> bool:
        return key in self.rows


# ---- candidates --------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One passage, and the thing about it this round would judge."""

    unit: str            # "review" or "tag"
    reason: str          # "downgraded" | "absence" | "untagged"
    pageid: int
    title: str
    campaign: str
    passage: dict
    location: dict | None
    base_version: int

    @property
    def passage_key(self) -> str:
        return str(self.passage.get("passage_key") or "")

    @property
    def pair_key(self) -> str | None:
        return str(self.location.get("pair_key")) if self.location and self.location.get("pair_key") else None

    @property
    def prominence(self) -> str:
        return str(self.passage.get("prominence") or "supporting")

    @property
    def kind(self) -> str:
        return str(self.passage.get("kind") or "other")

    def label(self) -> str:
        if self.location:
            return (f"{self.title} #{self.passage.get('item')} — {self.location.get('declaration')} "
                    f"({self.location.get('relation')}, {self.reason})")
        return f"{self.title} #{self.passage.get('item')} — not formalized ({self.reason})"


def _order(candidate: Candidate) -> tuple:
    """Weakest first, then most prominent, then a stable tie-break."""
    reason_rank = {"downgraded": 0, "absence": 1, "untagged": 2}
    return (
        reason_rank.get(candidate.reason, 9),
        PROMINENCE_ORDER.get(candidate.prominence, 3),
        0 if candidate.kind in FORMAL_KINDS else 1,
        str(candidate.passage.get("id") or ""),
        str(candidate.location.get("location_key") if candidate.location else ""),
    )


def _usable(passage: dict) -> bool:
    """A passage the overlay can carry a row for at all."""
    return bool(passage.get("passage_key")) and passage.get("outcome") != "unresolved"


def review_candidates(article: dict, overlay: Overlay) -> list[Candidate]:
    """Claims a second pass already touched, weakest first."""
    pageid = int(article.get("pageid") or 0)
    title = str(article.get("title") or "")
    campaign = str(article.get("campaign") or "")
    out: list[Candidate] = []
    for passage in _list(article.get("passages")):
        if not isinstance(passage, dict) or not _usable(passage):
            continue
        for location in _list(passage.get("locations")):
            if not isinstance(location, dict) or location.get("challenge") != CHALLENGE_DOWNGRADED:
                continue
            key = location.get("pair_key")
            if not isinstance(key, str) or not key:
                continue
            if overlay.written_by_a_person(key) or overlay.touched(key):
                continue
            out.append(Candidate("review", "downgraded", pageid, title, campaign, passage,
                                 location, overlay.version(key)))
        absence = _dict(passage.get("absence"))
        if passage.get("outcome") == "absent" and absence.get("challenge") == CHALLENGE_SURVIVED:
            out.append(Candidate("review", "absence", pageid, title, campaign, passage, None, 0))
    return sorted(out, key=_order)


def tag_candidates(article: dict, overlay: Overlay) -> list[Candidate]:
    """Formal passages with no record and no judged absence: nobody's claim yet."""
    pageid = int(article.get("pageid") or 0)
    title = str(article.get("title") or "")
    campaign = str(article.get("campaign") or "")
    out: list[Candidate] = []
    for passage in _list(article.get("passages")):
        if not isinstance(passage, dict) or not _usable(passage):
            continue
        if passage.get("outcome") != "absent" or _list(passage.get("locations")):
            continue
        if _dict(passage.get("absence")).get("challenge") == CHALLENGE_SURVIVED:
            continue  # the review unit's: a checked absence is a claim
        if any(row.get("passage_key") == passage.get("passage_key") for row in overlay.rows.values()):
            continue
        out.append(Candidate("tag", "untagged", pageid, title, campaign, passage, None, 0))
    return sorted(out, key=_order)


# ---- which article ------------------------------------------------------------


def index_entries(shards: Iterable[list[dict]]) -> list[dict]:
    rows: list[dict] = []
    for shard in shards:
        rows.extend(entry for entry in shard if isinstance(entry, dict) and entry.get("pageid"))
    return rows


def open_work(entries: Iterable[dict]) -> dict[str, int]:
    """What the index says is open, for the dashboard and `status`."""
    totals = {"articles": 0, "passages": 0, "not_formalized": 0, "partial": 0, "unresolved": 0}
    for entry in entries:
        totals["articles"] += 1
        for key in ("passages", "not_formalized", "partial", "unresolved"):
            value = entry.get(key)
            if isinstance(value, int) and value >= 0:
                totals[key] += value
    return totals


def article_order(entries: Iterable[dict], *, skip: Iterable[int] = ()) -> list[dict]:
    """Articles most worth a visit first: the most unsettled passages.

    Deterministic, so two contributors starting at the same cursor do not both
    walk the list from the same end; the cursor in state.py is what moves them
    apart.
    """
    blocked = set(skip)
    rows = [entry for entry in entries if int(entry.get("pageid") or 0) not in blocked]
    return sorted(
        rows,
        key=lambda e: (-(_int(e, "not_formalized") + _int(e, "partial")), -_int(e, "passages"),
                       int(e.get("pageid") or 0)),
    )


def _int(entry: dict, key: str) -> int:
    value = entry.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def libraries_of(universe: dict) -> dict[str, str]:
    """Library id → repository URI, from the article's own universe.

    The repository is what the pair key hashes; the library id is what a tag
    body names. A library with neither is not offered.
    """
    out: dict[str, str] = {}
    for library in _list(universe.get("libraries")):
        if not isinstance(library, dict):
            continue
        identifier, repo = library.get("id"), library.get("repo")
        if isinstance(identifier, str) and identifier and isinstance(repo, str) and repo:
            out[identifier] = repo
    return out
