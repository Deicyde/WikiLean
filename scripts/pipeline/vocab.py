#!/usr/bin/env python3
"""The locate vocabulary: the values a record, an outcome row and a seal may carry.

docs/PIPELINE.md section 3. One definition per value, for the whole pipeline. The
consumer's copy is in ``scripts/articles/build_store.py``, which checks a set against
it; ``scripts/pipeline/test_vocab.py`` pins the two together, so a value added on one
side fails until it is added on the other.

The record-set format itself is docs/ARTICLES.md section 3, and these are its words.
Nothing here is WikiLean's invention: they are the agreed format's, which is why the
names match the consumer's rather than reading more naturally.
"""
from __future__ import annotations

import re

#: The one extension namespace a locate record uses.
LOCATE = "org.mathlib-initiative.formal-companion.locate"

#: The seal's schema, by the status rule its statuses were derived under. /1 is status
#: rule 1 (decision D1); /2 carries the revision-free keys and a coverage claim as a
#: bundle, and its seal's ``status_rule`` is 2 or 3. Status rule 3 (R-BROAD) writes /2's
#: shapes: only the statuses differ.
SEAL_SCHEMA = "cf-locate-recordset/1"
SEAL_SCHEMA_BUNDLE = "cf-locate-recordset/2"
STATUS_RULE_D1 = 1
STATUS_RULE_BUNDLE = 2
STATUS_RULE_BROAD = 3
STATUS_RULES = (STATUS_RULE_D1, STATUS_RULE_BUNDLE, STATUS_RULE_BROAD)
BUNDLE_STATUS_RULES = (STATUS_RULE_BUNDLE, STATUS_RULE_BROAD)
SEAL_SCHEMAS = {STATUS_RULE_D1: SEAL_SCHEMA,
                STATUS_RULE_BUNDLE: SEAL_SCHEMA_BUNDLE,
                STATUS_RULE_BROAD: SEAL_SCHEMA_BUNDLE}

#: A passage's own kind (proposal section 2.2).
KINDS = frozenset({"definition", "theorem", "conjecture", "example", "construction", "notation", "other"})
#: Which row of the derived-status table judges a passage of that kind. A construction or
#: a notation introduces an object or a symbol, so it is a definition passage; an example
#: or an ``other`` passage is judged by both rows, the better winning.
THEOREM_KINDS = frozenset({"theorem", "conjecture"})
DEFINITION_KINDS = frozenset({"definition", "construction", "notation"})
EITHER_KINDS = frozenset({"example", "other"})

#: How prominent the passage is in its article (proposal section 2.2).
PROMINENCES = frozenset({"subject", "central", "supporting"})
PROMINENCE_SUBJECT = "subject"

#: A location's relation, subject = passage, object = declaration.
LOCATION_RELATIONS = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch")
#: Relations that state only part of the passage: partial on either row.
PART_RELATIONS = frozenset({"hasPart", "narrowMatch"})
#: Relations whose match to data formalizes a theorem passage (decision D1).
DATA_MATCH_RELATIONS = frozenset({"exactMatch", "closeMatch"})
RELATED_MATCH = "relatedMatch"
NO_MATCH = "noMatch"
CLOSE_MATCH = "closeMatch"
HAS_PART = "hasPart"

#: A declaration's proof status, decided mechanically at index time, never by a model.
PROOF_STATUSES = frozenset({"proved", "statement_only", "axiom_dependent", "wanted", "definition"})
#: Statuses that state something without a finished proof.
UNPROVED = frozenset({"statement_only", "wanted", "axiom_dependent"})
#: A location counts as complete when its declaration is proved or is a definition.
COMPLETE = frozenset({"proved", "definition"})

#: A passage's derived status, best first.
STATUSES = ("formalized", "statement_only", "partial", "not_formalized")
STATUS_RANK = {status: rank for rank, status in enumerate(STATUSES)}
NOT_FORMALIZED = "not_formalized"
#: What the store counts a passage with no derived status as.
UNRESOLVED = "unresolved"

#: A record's challenge value. The last three are records no challenge judged;
#: ``challenge_pending`` is one a prominence overturn put under a rule after the
#: challenge ran, so a verdict is owed and none has judged it.
CHALLENGES = frozenset({"survived", "downgraded", "not_required", "sampled_out", "challenge_pending"})
CHALLENGE_SURVIVED = "survived"
CHALLENGE_PENDING = "challenge_pending"
#: A part of a survived claim whose own challenge is one of these reads survived: the
#: claim confirmed it. A part another verdict judged keeps it, and a pending part stays
#: pending — the reopen wins over the claim.
CONFIRMED_WITH_CLAIM = frozenset({"not_required", "sampled_out"})
CONFIDENCES = frozenset({"high", "medium", "low"})

#: A coverage claim as a record carries it (decision D1), and as a /2 bundle.
COVERAGE_KEYS = ("complete", "parts", "challenge")
COVERAGE_KEYS_BUNDLE = (*COVERAGE_KEYS, "relation", "conjuncts", "bundle_key")
BUNDLE_RELATIONS = frozenset({"exactMatch", "closeMatch", "broadMatch"})
CONJUNCT_KEYS = frozenset({"id", "text", "part"})
MIN_CONJUNCTS = 2
MAX_CONJUNCTS = 5
MAX_CONJUNCT_CHARS = 1000
COVERAGE_RELATION = "hasPart"
MIN_COVERAGE_PARTS = 2
#: The relation a survived claim stands in as under status rule 1; under 2 and 3 it is
#: the bundle's own relation, its weakest part's.
COVERAGE_ACTS_AS = "exactMatch"
#: ``derive``'s basis when the claim, not one location, justifies the status.
COVERAGE_BASIS = "coverage"

#: Caps on what an exported record may quote (proposal sections 2.7 and 10).
QUOTE_MAX_WORDS = 12
AFFIX_MAX_CHARS = 32
#: At most K locations per passage, with distinct ranks.
MAX_LOCATIONS = 5
RANKS = range(1, 6)
#: Gate rule 5: an absence's receipt lists at least this many distinct queries.
MIN_ABSENCE_QUERIES = 3

#: An absence's lean end, and the receipt it references.
ABSENCE_END_KEYS = ("type", "uri", "version")
RECEIPT_KEYS = ("sha256", "queries", "candidates")
#: The keys of a receipt candidate the sidecar keeps.
RECEIPT_CANDIDATE_KEYS = ("library", "declaration", "verdict", "reason")

#: The locate extension's keys at each of the three places a record carries it.
RECORD_EXT_KEYS = ("item", "universe", "universe_sha256", "challenge", "section", "prominence")
RECORD_EXT_OPTIONAL = ("coverage",)
RECORD_EXT_KEYS_BUNDLE = (*RECORD_EXT_KEYS, "passage_key")
SHAPE_EXT_KEYS = {"location": ("location_key", "rank", "confidence"), "absence": ("receipt",), "unresolved": ()}
SHAPE_EXT_KEYS_BUNDLE = {"location": (*SHAPE_EXT_KEYS["location"], "pair_key"),
                         "absence": ("receipt",), "unresolved": ()}
SUBJECT_EXT_KEYS = ("pageid", "html_sha256", "renderer", "span_sha256", "licence")
SUBJECT_EXT_OPTIONAL = ("span", "layer_sha256")
LEAN_EXT_KEYS = ("library", "path", "line", "proof_status")
LEAN_EXT_OPTIONAL = ("end_line", "statement")

#: The seal's keys, exactly (docs/ARTICLES.md section 3).
SEAL_KEYS = ("schema", "set_id", "campaign", "universe", "sourceset", "skill_commit", "created",
             "byte_budget", "files")
SEAL_KEYS_BUNDLE = (*SEAL_KEYS, "status_rule")
SEAL_UNIVERSE_KEYS = ("id", "universe_sha256")
SEAL_SOURCESET_KEYS = ("id", "manifest_sha256")
UNIVERSE_KEYS = ("id", "universe_sha256", "frozen", "libraries", "excluded")
LIBRARY_KEYS = ("id", "brain_lib", "repo", "commit", "toolchain", "roots", "licence")
LIBRARY_OPTIONAL = ("display_name",)
EXCLUDED_KEYS = ("id", "reason")
SOURCE_KEYS = ("source_id", "pageid", "revision", "title", "wikidata", "html_sha256",
               "layer_sha256", "renderer", "assessment")

#: Licence classes an exported record may quote from, and the ones that need the
#: licence named (formal-companion POLICY.md section 4.1, the agreed classes).
OPEN_LICENCE_CLASSES = frozenset({"public_domain", "open_permissive", "open_share_alike"})
ATTRIBUTION_LICENCE_CLASSES = frozenset({"open_permissive", "open_share_alike"})

HEX40 = re.compile(r"^[0-9a-f]{40}\Z")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
ITEM_RE = re.compile(r"^In-text #[0-9a-f]{8,16}\Z")
SOURCE_ID_RE = re.compile(r"^enwiki-([1-9][0-9]*)-([1-9][0-9]*)\Z")
#: A record-set token: ``set_id``, ``universe.id``, ``sourceset.id``, a library or
#: exclusion id.
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TOKEN_RULE = "a token: a letter or digit, then letters, digits and ._:- (at most 128 characters)"
#: A run id inside a run token.
RUN_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
RUN_ID_RE = re.compile(rf"^{RUN_ID}\Z")
#: ``SEAL.json``'s campaign: a campaign id, or ``run:<run>[+<challenge run>]``.
CAMPAIGN_RE = re.compile(rf"^(?:run:{RUN_ID}(?:\+{RUN_ID})?|(?!run:)[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}})\Z")
RUN_TOKEN_PREFIX = "run:"
CREATED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
REPO_RE = re.compile(r"^https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[A-Za-z0-9_.~-]{1,100}){1,4}\Z")
RENDERER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,63}\Z")
QID_RE = re.compile(r"^Q[1-9][0-9]*\Z")
#: A library's ``display_name``, as the consumer will show it.
DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9 ,.()\u00b7/+:'&_-]{1,100}\Z")
MAX_NAME_CHARS = 300
MAX_REASON_CHARS = 1000


def is_repo_url(uri) -> bool:
    """A library repository the consumer can name and link: :data:`REPO_RE`, no dot segments.

    The grammar alone admits ``.`` and ``..`` as path segments, which a browser resolves
    out of the link, so the segments are checked beside it (build_store.is_repo_url).
    """
    return (isinstance(uri, str) and REPO_RE.match(uri) is not None
            and all(part not in {".", ".."} for part in uri.split("/")[3:]))


def member(value, allowed) -> bool:
    """Whether ``value`` is one of ``allowed`` — a str, never a bool or a number that compares equal."""
    return isinstance(value, str) and value in allowed


def status_rule_problem(status_rule) -> str | None:
    """Why ``status_rule`` names no derivation this pipeline knows, or None."""
    if type(status_rule) is not int or status_rule not in STATUS_RULES:
        return f"status rule {status_rule!r} is not one of {list(STATUS_RULES)}"
    return None
