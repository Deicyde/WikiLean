"""Shared cross-reference database specs for Mathlib tag automation.

The batch bot historically treated every item as a Wikidata QID.  Keep that
shape as a backwards-compatible shorthand, but normalize internally to the
database-agnostic `{db, id}` pair used by the review/queue path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, MutableMapping


@dataclass(frozen=True)
class CrossRefSpec:
    db: str
    attr: str
    label: str
    id_re: str
    url: str

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(rf"\b{re.escape(self.attr)}\s+({self.id_re})\b")

    def valid(self, ident: str) -> bool:
        return re.fullmatch(self.id_re, ident) is not None

    def link(self, ident: str) -> str:
        return self.url + ident


SPECS: dict[str, CrossRefSpec] = {
    "wikidata": CrossRefSpec(
        db="wikidata",
        attr="wikidata",
        label="Wikidata",
        id_re=r"Q\d+",
        url="https://www.wikidata.org/wiki/",
    ),
    "lmfdb": CrossRefSpec(
        db="lmfdb",
        attr="lmfdb",
        label="LMFDB",
        id_re=r"[a-z0-9_.]+",
        url="https://www.lmfdb.org/knowledge/show/",
    ),
    # Not queue targets yet, but known CrossRefAttribute attrs.  Including them
    # lets leak guards and import-pruning avoid assuming every crossref is
    # Wikidata-shaped.
    "stacks": CrossRefSpec(
        db="stacks",
        attr="stacks",
        label="Stacks",
        id_re=r"[0-9A-Z]{4}",
        url="https://stacks.math.columbia.edu/tag/",
    ),
    "kerodon": CrossRefSpec(
        db="kerodon",
        attr="kerodon",
        label="Kerodon",
        id_re=r"[0-9A-Z]{4}",
        url="https://kerodon.net/tag/",
    ),
}

DEFAULT_DB = "wikidata"


def spec(db: str | None) -> CrossRefSpec:
    key = (db or DEFAULT_DB).lower()
    if key not in SPECS:
        raise ValueError(f"unknown crossref database: {db}")
    return SPECS[key]


def normalize_tag(row: Mapping[str, object], default_db: str = DEFAULT_DB) -> dict:
    """Return a mutable tag row with canonical db/id fields.

    Legacy Wikidata rows may contain only `qid`; callers keep receiving `qid`
    as an alias for compatibility.
    """
    db = str(row.get("db") or default_db or DEFAULT_DB).lower()
    sp = spec(db)
    ident = row.get("id") or row.get("tag") or row.get("qid")
    if not isinstance(ident, str) or not sp.valid(ident):
        raise ValueError(f"bad {sp.label} id: {ident!r}")
    out = dict(row)
    out["db"] = db
    out["id"] = ident
    if db == "wikidata":
        out["qid"] = ident
    return out


def tag_text(row: Mapping[str, object] | str, ident: str | None = None) -> str:
    if isinstance(row, str):
        sp = spec(row)
        if ident is None:
            raise ValueError("ident required when row is a database name")
        return f"{sp.attr} {ident}"
    norm = normalize_tag(row)
    return f"{spec(norm['db']).attr} {norm['id']}"


def tag_label(row: Mapping[str, object]) -> str:
    norm = normalize_tag(row)
    return str(norm["qid"] if norm["db"] == "wikidata" else norm["id"])


def all_attr_regex() -> re.Pattern[str]:
    parts = [rf"{re.escape(s.attr)}\s+{s.id_re}" for s in SPECS.values()]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


def tag_regex(db: str = DEFAULT_DB) -> re.Pattern[str]:
    return spec(db).regex


def find_ids(text: str, db: str = DEFAULT_DB) -> list[str]:
    return [m.group(1) for m in tag_regex(db).finditer(text)]


def marker(db: str, ident: str) -> str:
    return f"{spec(db).db}:{ident}"


def marker_id(db: str, ident: str) -> str:
    """Stable key for review verdict maps."""
    return marker(db, ident)


def parse_review_marker(body: str, db: str = DEFAULT_DB) -> str | None:
    sp = spec(db)
    if sp.db == "wikidata":
        old = re.search(r"wikilean-review:(Q\d+)", body)
        if old:
            return old.group(1)
    m = re.search(rf"wikilean-review:{re.escape(sp.db)}:({sp.id_re})\b", body)
    return m.group(1) if m else None


def parse_crossref_bot_marker(body: str, db: str = DEFAULT_DB) -> str | None:
    sp = spec(db)
    if sp.db == "wikidata":
        old = re.search(r"crossref-bot:(Q\d+)", body)
        if old:
            return old.group(1)
    m = re.search(rf"crossref-bot:{re.escape(sp.db)}:({sp.id_re})\b", body)
    return m.group(1) if m else None


def coerce_ids(ids: list[str], db: str = DEFAULT_DB) -> list[str]:
    sp = spec(db)
    out: list[str] = []
    for ident in ids:
        ident = ident.strip()
        if not ident:
            continue
        if not sp.valid(ident):
            raise ValueError(f"bad {sp.label} id: {ident}")
        out.append(ident)
    return out


def clean_item(row: MutableMapping[str, object], default_db: str = DEFAULT_DB) -> MutableMapping[str, object]:
    norm = normalize_tag(row, default_db)
    row["db"] = norm["db"]
    row["id"] = norm["id"]
    if norm["db"] == "wikidata":
        row["qid"] = norm["id"]
    return row
