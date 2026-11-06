#!/usr/bin/env python3
"""Stage 3: the derived status of a passage, under status rules 1, 2 and 3.

docs/PIPELINE.md section 3 (stage 3); the rules themselves are docs/ARTICLES.md section
3, check 6, which states the table, the maintainer's decision D1 (2026-10-18), the
round-three reading of it and decision R-BROAD (2026-10-28).

This is the **producer's** side: the tally stamps the status every record of a passage
carries, and ``scripts/articles/build_store.py`` re-derives it and refuses a set that
disagrees. So the two must give the same answer on every input, and
``scripts/pipeline/test_status.py`` holds them together on all three of the producer's
truth tables, all three of the hand-written shared tables, and every passage of a real
record set.

The table, per location that counts (a ``relatedMatch`` never counts):

| location                                     | theorem, conjecture | definition, construction, notation |
|----------------------------------------------|---------------------|------------------------------------|
| hasPart or narrowMatch, to anything           | partial             | partial                            |
| broadMatch to data                            | partial (rule 3: formalized) | partial (rule 3: formalized) |
| broadMatch to anything else                   | as an exact match   | partial                            |
| exact/close match to a proved declaration     | formalized          | partial                            |
| exact/close to statement_only/wanted/axiom_dependent | statement only | statement only                  |
| exact/close to a Prop-valued definition       | statement only      | formalized                         |
| exact/close to any other definition (data)    | formalized (D1)     | formalized                         |

An example or ``other`` passage is judged by both rows and takes the better answer. A
survived coverage claim acts as **one more location**, a fixed stand-in: proved on the
theorem row and data on the definition row when every part is finished, statement_only
on both rows otherwise; its relation is ``exactMatch`` under rule 1 and the bundle's own
relation (its weakest part's) under rules 2 and 3. The passage takes the best over all
of them; a checked absence is ``not_formalized``; a passage the table gives nothing is
unresolved, and its records carry no status.

``derive`` is the entry the shared parity tables are written for, with the producer's own
signature and the consumer's refusals: where a set would be refused it raises
:class:`StatusProblem` rather than mapping the contradiction to a status.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import vocab  # noqa: E402


class StatusProblem(ValueError):
    """An input the status rules refuse rather than derive a status from."""


# ---- one location, one row -----------------------------------------------------------


def _theorem_row(loc: dict, status_rule: int = vocab.STATUS_RULE_D1) -> str:
    """The status one location gives a theorem or conjecture passage."""
    if loc["relation"] in vocab.PART_RELATIONS:
        return "partial"
    # exactMatch, closeMatch, broadMatch
    if loc["statement"]:
        # a Prop-valued definition states a theorem and proves none
        return "statement_only"
    if loc["proof_status"] == "proved":
        return "formalized"
    if loc["proof_status"] in vocab.UNPROVED:
        return "statement_only"
    # Data (a definition without the statement flag). Decision D1 makes an exact or close
    # match to an equivalence or data declaration that states the theorem formalized (CRT's
    # RingEquiv); a broadMatch to data stays partial under rules 1 and 2 and formalizes it
    # under 3 (R-BROAD: among the strong three the relation alone decides, never the
    # declaration's kind). Data resting on sorry has proof_status statement_only, above.
    if loc["relation"] in vocab.DATA_MATCH_RELATIONS or status_rule >= vocab.STATUS_RULE_BROAD:
        return "formalized"
    return "partial"


def _definition_row(loc: dict, status_rule: int = vocab.STATUS_RULE_D1) -> str:
    """The status one location gives a definition, construction or notation passage."""
    if loc["relation"] in vocab.PART_RELATIONS:
        return "partial"
    if loc["relation"] == "broadMatch":
        # R-BROAD: a broadMatch to a definition formalizes it, as an exact or close match
        # does; a broadMatch to anything else stays partial, as under rules 1 and 2.
        if status_rule >= vocab.STATUS_RULE_BROAD and loc["proof_status"] == "definition":
            return "formalized"
        return "partial"
    # exactMatch, closeMatch
    if loc["proof_status"] == "definition":
        return "formalized"
    if loc["proof_status"] in vocab.UNPROVED:
        return "statement_only"
    # A proved theorem stating a definition passage: not in the table; vocabulary.md
    # section 1.11 decides partial.
    return "partial"


def location_status(kind: str, loc: dict, status_rule: int = vocab.STATUS_RULE_D1) -> str | None:
    """The status one location gives a passage of ``kind``; None for a relatedMatch."""
    if loc["relation"] == vocab.RELATED_MATCH:
        return None
    rows = []
    if kind in vocab.THEOREM_KINDS or kind in vocab.EITHER_KINDS:
        rows.append(_theorem_row(loc, status_rule))
    if kind in vocab.DEFINITION_KINDS or kind in vocab.EITHER_KINDS:
        rows.append(_definition_row(loc, status_rule))
    return min(rows, key=vocab.STATUS_RANK.__getitem__) if rows else None


# ---- the coverage claim ---------------------------------------------------------------


def part_finished(part: dict) -> bool:
    """Whether a claim's part is finished, by the round-three reading of D1.

    Finished: proved, or data (a definition without the statement flag). Unfinished:
    statement_only, wanted, axiom_dependent, or a Prop-valued definition whatever its
    proof status — it states a clause and proves none.
    """
    return part["proof_status"] == "proved" or (part["proof_status"] == "definition"
                                                and not part["statement"])


def coverage_location(row: str, parts: list[dict],
                      relation: str = vocab.COVERAGE_ACTS_AS) -> dict:
    """The one location a survived claim acts as, on the theorem or the definition row.

    A fixed stand-in, not any one part's own proof status: every part finished makes it a
    proved declaration on the theorem row and data on the definition row, so the claim
    formalizes the passage on either (a data part counts, as an exact match to data does —
    Q2; and the definition row gets the statement-only case an exact match to an unfinished
    declaration has — Q3). Any part unfinished makes it statement_only on both rows.

    "The least finished part" read literally would be wrong on the definition row: a claim
    of proved parts formalizes a definition passage, where an exact match to a proved
    theorem alone is partial, and one with a Prop-valued-definition part is statement only,
    where an exact match to that definition alone formalizes it.
    """
    if not all(part_finished(part) for part in parts):
        proof_status = "statement_only"
    else:
        proof_status = "proved" if row == "theorem" else "definition"
    return {"relation": relation, "proof_status": proof_status, "statement": False}


def coverage_status(kind: str, claim: dict | None, locations: list[dict],
                    status_rule: int = vocab.STATUS_RULE_D1) -> str | None:
    """The status a coverage claim gives a passage on its own; None when it gives none.

    Only a survived claim counts: one pending, unjudged (not_required, sampled_out) or
    downgraded upgrades nothing, and a refuted claim is not exported at all.
    """
    if claim is None or claim["challenge"] != vocab.CHALLENGE_SURVIVED:
        return None
    by_key = {loc["location_key"]: loc for loc in locations}
    parts = [by_key.get(key) for key in claim["parts"]]
    if len(parts) < vocab.MIN_COVERAGE_PARTS or any(
            part is None or part["relation"] != vocab.COVERAGE_RELATION for part in parts):
        return None  # refused by coverage_part_problems; never counted
    relation = claim["relation"] if status_rule >= vocab.STATUS_RULE_BUNDLE else vocab.COVERAGE_ACTS_AS
    rows = []
    if kind in vocab.THEOREM_KINDS or kind in vocab.EITHER_KINDS:
        rows.append(_theorem_row(coverage_location("theorem", parts, relation), status_rule))
    if kind in vocab.DEFINITION_KINDS or kind in vocab.EITHER_KINDS:
        rows.append(_definition_row(coverage_location("definition", parts, relation), status_rule))
    return min(rows, key=vocab.STATUS_RANK.__getitem__) if rows else None


def confirmed_parts(claim: dict, locations: list[dict]) -> list[dict]:
    """The passage's locations with a survived claim's unjudged parts confirmed.

    Confirming a claim confirms its parts: a part whose own challenge is ``not_required``
    or ``sampled_out`` reads ``survived`` once the claim survived. A part another verdict
    judged keeps it, and a ``challenge_pending`` part stays pending — a prominence
    overturn put it under a rule requiring its own verdict, and the reopen wins.
    """
    if claim["challenge"] != vocab.CHALLENGE_SURVIVED:
        return locations
    parts = set(claim["parts"])
    return [{**loc, "challenge": vocab.CHALLENGE_SURVIVED}
            if loc["location_key"] in parts and loc["challenge"] in vocab.CONFIRMED_WITH_CLAIM
            else loc
            for loc in locations]


# ---- the passage ----------------------------------------------------------------------


def derive_with_basis(kind: str, locations: list[dict], coverage: dict | None = None,
                      status_rule: int = vocab.STATUS_RULE_D1) -> tuple[str | None, Any]:
    """``(status, basis)`` of a located passage: the best over every piece of evidence.

    ``basis`` is the highest-ranked location that reaches the status, or
    ``vocab.COVERAGE_BASIS`` when only the claim reaches it (a location reaching the same
    status is preferred). ``locations`` are in rank order. ``(None, None)`` means the
    table gives the passage no status: its only locations are relatedMatch.
    """
    best: str | None = None
    basis: Any = None
    for loc in locations:
        status = location_status(kind, loc, status_rule)
        if status is not None and (best is None or vocab.STATUS_RANK[status] < vocab.STATUS_RANK[best]):
            best, basis = status, loc
    claimed = coverage_status(kind, coverage, locations, status_rule)
    if claimed is not None and (best is None or vocab.STATUS_RANK[claimed] < vocab.STATUS_RANK[best]):
        best, basis = claimed, vocab.COVERAGE_BASIS
    return best, basis


# ---- the checked entry the parity tables are written for -------------------------------


def _statement_flag(value: Any, proof_status: str | None) -> bool:
    """The lean end's optional Prop-valued-definition flag; absent or null is false."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise StatusProblem("statement must be true or false (the library index sets it on a "
                            "Prop-valued definition)")
    if value and proof_status == "proved":
        raise StatusProblem("a proved declaration with the statement flag set: the flag marks a "
                            "Prop-valued definition, whose proof_status is definition, wanted, "
                            "statement_only or axiom_dependent")
    return value


def _claim(coverage: Any, status_rule: int) -> dict:
    """A coverage claim's own shape, as a record carries it; refuses rather than guesses.

    Under status rule 1 exactly ``{complete, parts, challenge}``; under 2 and 3 a bundle,
    which may also carry ``relation`` (required: the derivation reads it), ``conjuncts``
    and ``bundle_key``. The shapes of the last two are checked when given, as the
    exporter's own check does; that a part is a hasPart location of this passage is
    :func:`_part_problems`.
    """
    bundled = status_rule >= vocab.STATUS_RULE_BUNDLE
    if not isinstance(coverage, dict):
        raise StatusProblem("a coverage claim is an object "
                            f"{{{', '.join(vocab.COVERAGE_KEYS_BUNDLE if bundled else vocab.COVERAGE_KEYS)}}}")
    allowed = set(vocab.COVERAGE_KEYS_BUNDLE if bundled else vocab.COVERAGE_KEYS)
    missing = sorted(set(vocab.COVERAGE_KEYS) - set(coverage))
    extra = sorted(set(coverage) - allowed)
    if missing:
        raise StatusProblem(f"the coverage claim has no {', '.join(missing)}")
    if extra:
        raise StatusProblem(f"the coverage claim has unexpected key(s) {', '.join(extra)}")
    if coverage["complete"] is not True:
        raise StatusProblem("coverage.complete must be true: the claim is that the parts together "
                            "state every clause")
    parts = coverage["parts"]
    if not isinstance(parts, list) or not all(isinstance(key, str) and vocab.HEX64.match(key)
                                              for key in parts):
        raise StatusProblem("coverage.parts must be a list of location_keys (64 hex), one per "
                            "hasPart location")
    if len(set(parts)) != len(parts):
        raise StatusProblem("coverage.parts names one location twice")
    if len(parts) < vocab.MIN_COVERAGE_PARTS:
        raise StatusProblem(f"coverage.parts names {len(parts)} location(s): a coverage claim needs "
                            f"at least {vocab.MIN_COVERAGE_PARTS} hasPart locations")
    challenge = coverage["challenge"]
    if challenge is None:
        raise StatusProblem("coverage.challenge is null: a record's claim carries its challenge "
                            "value, and an owed claim is not exported")
    if not vocab.member(challenge, vocab.CHALLENGES):
        raise StatusProblem(f"coverage.challenge must be one of {sorted(vocab.CHALLENGES)} "
                            "(only survived counts)")
    out = {"complete": True, "parts": list(parts), "challenge": challenge}
    if not bundled:
        return out
    if "relation" not in coverage:
        raise StatusProblem("coverage.relation is missing: a bundle carries its relation, its "
                            "weakest part's")
    if not vocab.member(coverage["relation"], vocab.BUNDLE_RELATIONS):
        raise StatusProblem(f"coverage.relation must be one of {sorted(vocab.BUNDLE_RELATIONS)} "
                            "(the bundle's, its weakest part's)")
    out["relation"] = coverage["relation"]
    if "conjuncts" in coverage:
        out["conjuncts"] = _conjuncts(coverage["conjuncts"], parts)
    if "bundle_key" in coverage:
        key = coverage["bundle_key"]
        if not isinstance(key, str) or not vocab.HEX64.match(key):
            raise StatusProblem("coverage.bundle_key must be 64 lowercase hex: the sha256 of the "
                                "passage key and the parts' pair keys, sorted, one per line")
        out["bundle_key"] = key
    return out


def _conjuncts(conjuncts: Any, parts: list[str]) -> list[dict]:
    """``[{id, text, part}]``, ids c1..cN in order, the parts partitioning them."""
    if not isinstance(conjuncts, list) or not vocab.MIN_CONJUNCTS <= len(conjuncts) <= vocab.MAX_CONJUNCTS:
        raise StatusProblem(f"coverage.conjuncts must be {vocab.MIN_CONJUNCTS} to "
                            f"{vocab.MAX_CONJUNCTS} {{id, text, part}} objects (a bundle stands "
                            "only on a compound statement)")
    named: set[str] = set()
    out = []
    for number, conjunct in enumerate(conjuncts, 1):
        if not isinstance(conjunct, dict) or set(conjunct) != vocab.CONJUNCT_KEYS:
            raise StatusProblem(f"coverage.conjuncts[{number - 1}] must be exactly {{id, text, part}}")
        if conjunct["id"] != f"c{number}":
            raise StatusProblem(f"coverage.conjuncts are c1..c{len(conjuncts)} in order; conjunct "
                                f"{number} is {conjunct['id']!r}")
        text = conjunct["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > vocab.MAX_CONJUNCT_CHARS:
            raise StatusProblem(f"coverage.conjuncts {conjunct['id']!r} text must be a non-empty "
                                f"string of at most {vocab.MAX_CONJUNCT_CHARS} characters")
        if conjunct["part"] not in parts:
            raise StatusProblem(f"coverage.conjuncts {conjunct['id']!r} names part "
                                f"{str(conjunct['part'])[:12]}, which is not one of the bundle's parts")
        named.add(conjunct["part"])
        out.append(dict(conjunct))
    idle = [key for key in parts if key not in named]
    if idle:
        raise StatusProblem(f"coverage: bundle part {idle[0][:12]} states no conjunct (a bundle's "
                            "parts partition its conjuncts, each stating at least one)")
    return out


def _part_problems(claim: dict, locations: list[dict]) -> None:
    """Refuse a claim whose parts are not hasPart locations of this passage."""
    relation_of = {loc["location_key"]: loc["relation"] for loc in locations}
    for key in claim["parts"]:
        relation = relation_of.get(key)
        if relation is None:
            raise StatusProblem(f"coverage names {key}, which is no location of this passage: "
                                f"every part is one of its {vocab.COVERAGE_RELATION} locations")
        if relation != vocab.COVERAGE_RELATION:
            raise StatusProblem(f"coverage names {key}, whose relation is {relation}: every part "
                                f"is a {vocab.COVERAGE_RELATION} location of this passage")


def locations_rows(locations: Any) -> list[dict]:
    """``locations`` as the rules read them, refusing what the format refuses.

    Each is ``{location_key, relation, proof_status, statement}``: a locate relation, a
    proof status, a bool-or-absent statement flag never set on a proved declaration, and
    a 64-hex location key no other location of the passage has.
    """
    if not isinstance(locations, (list, tuple)):
        raise StatusProblem("locations is a list of {location_key, relation, proof_status, statement}")
    rows: list[dict] = []
    for raw in locations:
        if not isinstance(raw, dict):
            raise StatusProblem("a location is an object {location_key, relation, proof_status, statement}")
        relation, proof_status = raw.get("relation"), raw.get("proof_status")
        if not vocab.member(relation, vocab.LOCATION_RELATIONS):
            raise StatusProblem(f"relation {relation!r} is not a locate relation")
        if not vocab.member(proof_status, vocab.PROOF_STATUSES):
            raise StatusProblem(f"proof_status must be one of {sorted(vocab.PROOF_STATUSES)}")
        statement = _statement_flag(raw.get("statement"), proof_status)
        key = raw.get("location_key")
        if not isinstance(key, str) or not vocab.HEX64.match(key):
            raise StatusProblem("location_key must be 64 hex")
        if any(row["location_key"] == key for row in rows):
            raise StatusProblem(f"location_key {key} appears twice")
        rows.append({"relation": relation, "proof_status": proof_status, "statement": statement,
                     "location_key": key})
    return rows


def derive(kind: Any, locations: Any, absent: bool = False, coverage: Any = None,
           status_rule: int = vocab.STATUS_RULE_D1) -> str | None:
    """One passage's derived status, by the signature the shared parity tables use.

    ``locations`` are ``{location_key, relation, proof_status, statement}`` mappings,
    ``absent`` says the passage has a checked absence, and ``coverage`` is the claim
    exactly as a record carries it, or None. Returns the status, ``not_formalized`` for a
    checked absence, or None when the table gives the passage none (unresolved).

    Raises :class:`StatusProblem` for an input a record set may not carry — a contradiction
    is refused, never mapped to a status.
    """
    problem = vocab.status_rule_problem(status_rule)
    if problem:
        raise StatusProblem(problem)
    if not vocab.member(kind, vocab.KINDS):
        raise StatusProblem(f"kind must be one of {sorted(vocab.KINDS)}")
    rows = locations_rows(locations)
    if absent and any(row["relation"] != vocab.RELATED_MATCH for row in rows):
        raise StatusProblem("an absence may sit beside relatedMatch locations only")
    claim = None
    if coverage is not None:
        claim = _claim(coverage, status_rule)
        _part_problems(claim, rows)
    if absent:
        return vocab.NOT_FORMALIZED
    return derive_with_basis(kind, rows, claim, status_rule)[0]
