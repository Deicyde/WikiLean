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
PROMINENCE_CENTRAL = "central"
PROMINENCE_SUPPORTING = "supporting"
#: The prominences under which a challenge refutes every location by default and hunts
#: the absence, so a prominence overturned into one of them reopens the passage's records.
REFUTE_BY_DEFAULT_PROMINENCE = frozenset({PROMINENCE_SUBJECT, PROMINENCE_CENTRAL})

#: What the inventory said the passage is: mathematics to locate, prose about it, or
#: neither. Only ``formal_math`` is located; the rest are scoped out of the run.
SCOPE_FORMAL_MATH = "formal_math"
SCOPE_EXPOSITORY = "expository"
SCOPE_NON_MATH = "non_math"
SCOPES = frozenset({SCOPE_FORMAL_MATH, SCOPE_EXPOSITORY, SCOPE_NON_MATH})
LOCATED_SCOPES = frozenset({SCOPE_FORMAL_MATH})

#: A location's relation, subject = passage, object = declaration.
LOCATION_RELATIONS = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch")
#: Relations that state only part of the passage: partial on either row.
PART_RELATIONS = frozenset({"hasPart", "narrowMatch"})
#: Relations whose match to data formalizes a theorem passage (decision D1).
DATA_MATCH_RELATIONS = frozenset({"exactMatch", "closeMatch"})
RELATED_MATCH = "relatedMatch"
NO_MATCH = "noMatch"
EXACT_MATCH = "exactMatch"
CLOSE_MATCH = "closeMatch"
BROAD_MATCH = "broadMatch"
NARROW_MATCH = "narrowMatch"
HAS_PART = "hasPart"
#: A relation's strength, strongest first: a challenge ``downgrade`` names a WEAKER one,
#: and under status rule 3 the surviving cards are ranked by it. One order for the whole
#: pipeline — the gate's, the tally's and the bundle's weakest-part rule.
RELATION_STRENGTH = {EXACT_MATCH: 0, CLOSE_MATCH: 1, BROAD_MATCH: 2, NARROW_MATCH: 3,
                     HAS_PART: 3, RELATED_MATCH: 4}
#: The three relations that state the whole passage: a location held to one of them
#: carries the two-way ``tests``, and a bundle's relation is one of them.
STRONG_RELATIONS = frozenset({EXACT_MATCH, CLOSE_MATCH, BROAD_MATCH})
#: A relation whose record names the step it states.
STEP_RELATIONS = frozenset({HAS_PART, NARROW_MATCH})
#: The relation a part may bear to the conjuncts it states (judgment rules 3), and the
#: same with ``hasPart``, which rules 4 admit on a part that states only some of them.
CONJUNCT_RELATIONS = frozenset({EXACT_MATCH, CLOSE_MATCH, BROAD_MATCH, NARROW_MATCH})
CONJUNCT_RELATIONS_PART = CONJUNCT_RELATIONS | {HAS_PART}
#: What a locate document may say of a location beyond the format's own keys (the
#: judgment rules); the last three are judgment rules 3's and 4's, read only on a run
#: whose status rule reads bundles.
JUDGMENT_LOCATION_FIELDS = ("clauses", "instantiation", "step", "convention",
                            "conjunct_relation", "tests", "pd6")
#: What a downgrade to ``relatedMatch`` says of the record it leaves (judgment rules 4).
BRIDGE_FIELDS = ("bridge", "incomparable")

#: A declaration's proof status, decided mechanically at index time, never by a model.
PROOF_STATUSES = frozenset({"proved", "statement_only", "axiom_dependent", "wanted", "definition"})
#: Statuses that state something without a finished proof.
UNPROVED = frozenset({"statement_only", "wanted", "axiom_dependent"})
#: A location counts as complete when its declaration is proved or is a definition.
COMPLETE = frozenset({"proved", "definition"})

#: A harvested document's ``mode``: which of the three model stages wrote it.
MODE_INVENTORY = "locate-inventory"
MODE_LOCATE = "locate"
MODE_CHALLENGE = "locate-challenge"
MODES = frozenset({MODE_INVENTORY, MODE_LOCATE, MODE_CHALLENGE})
#: The schema a harvested document of any of the three modes carries.
DOCUMENT_SCHEMA = "cf-locate/1"
#: A challenge plan's schema.
CHALLENGE_PLAN_SCHEMA = "cf-locate-challenge-plan/1"

#: What a locate document says became of a passage.
OUTCOME_LOCATED = "located"
OUTCOME_ABSENT = "absent"
OUTCOME_UNRESOLVED = "unresolved"
LOCATE_OUTCOMES = frozenset({OUTCOME_LOCATED, OUTCOME_ABSENT, OUTCOME_UNRESOLVED})
#: Who left a passage unresolved.
UNRESOLVED_BY_WORKER = "worker"
UNRESOLVED_ATTEMPT_CAP = "attempt_cap"
#: What a worker who cannot re-cut an unresolved passage says of its cut (rules 3).
CUT_FLAG_NOT_A_STATEMENT = "not_a_statement"
CUT_FLAG_SEVERAL_STATEMENTS = "several_statements"
CUT_FLAGS = frozenset({CUT_FLAG_NOT_A_STATEMENT, CUT_FLAG_SEVERAL_STATEMENTS})

#: What a re-cut did to one passage of the old cut (the carry-over map, stage 8).
#: ``unchanged`` is the only fate a verdict, a gold label or a known answer carries over.
CARRY_UNCHANGED = "unchanged"
CARRY_END_MOVED = "end_moved"
CARRY_SPLIT = "split"
CARRY_MERGED = "merged"
CARRY_RESHAPED = "reshaped"
CARRY_REMOVED = "removed"
CARRY_CHANGED = "changed"
CARRY_FATES = frozenset({CARRY_UNCHANGED, CARRY_END_MOVED, CARRY_SPLIT, CARRY_MERGED,
                         CARRY_RESHAPED, CARRY_REMOVED, CARRY_CHANGED})
#: A passage of the new cut that no old passage went into.
CARRY_NEW = "new"

#: The finding-row kinds the tally keys on. Spelled in capitals because they are the
#: table's own names, and distinct from :data:`UNRESOLVED`, which is a passage's state.
ROW_PASSAGE = "PASSAGE"
ROW_LOCATION = "LOCATION"
ROW_ABSENCE = "ABSENCE"
ROW_UNRESOLVED = "UNRESOLVED"

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
CHALLENGE_DOWNGRADED = "downgraded"
CHALLENGE_NOT_REQUIRED = "not_required"
CHALLENGE_SAMPLED_OUT = "sampled_out"
CHALLENGE_PENDING = "challenge_pending"
#: A record no verdict judged. A prominence overturn reopens exactly these.
CHALLENGE_UNJUDGED = frozenset({CHALLENGE_NOT_REQUIRED, CHALLENGE_SAMPLED_OUT,
                                CHALLENGE_PENDING})
#: What a challenge plan says of a record it requires a verdict on; the other two values
#: a plan writes are ``not_required`` and ``sampled_out``, which are challenge values too.
CHALLENGE_REQUIRED = "required"
#: The verdict actions that keep the record they judge; the rest change or remove it.
CHALLENGE_KEEP = frozenset({"confirm", "uphold"})
#: The two actions a verdict on a coverage claim may take.
COVERAGE_ACTIONS = frozenset({"confirm", "refute"})
#: The challenge target of a coverage claim, as a plan and its verdicts name it.
COVERAGE_TARGET = "coverage"
#: What a located passage the challenge left with nothing that counts still owes.
CHALLENGE_RULE_HUNT = "hunt"
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
#: The shape of a compound statement, in the order a prompt lists them: the conditions of
#: one equivalence; the cases of one law or definition; several conclusions about one
#: subject under one hypothesis; one display holding several equations. An equation chain
#: is no compound — it is one statement, its end-to-end equation.
SHAPE_EXISTENCE_UNIQUENESS = "existence_uniqueness"
SHAPE_IFF = "iff"
SHAPE_CASES = "cases"
SHAPE_CONCLUSIONS = "conclusions"
SHAPE_EQUATIONS = "equations"
COMPOUND_SHAPE_ORDER = (SHAPE_EXISTENCE_UNIQUENESS, SHAPE_IFF, SHAPE_CASES,
                        SHAPE_CONCLUSIONS, SHAPE_EQUATIONS)
COMPOUND_SHAPES = frozenset(COMPOUND_SHAPE_ORDER)
#: A shape named here has exactly that many conjuncts.
SHAPE_CONJUNCT_COUNTS = {SHAPE_EXISTENCE_UNIQUENESS: 2}
#: The one clause id of a simple statement: the statement itself.
SIMPLE_STATEMENT_CLAUSE = "c1"
#: The closed list a ``closeMatch`` names its convention difference from. A
#: strengthening, a generalisation or a weakening is none of them, and since the
#: maintainer's PD7 (judgment rules 2) neither is a junk value: a declaration that agrees
#: with the passage on the passage's own domain is an ``exactMatch`` when every case
#: outside it is trivial, and a ``closeMatch`` differs where both sides are defined.
CONVENTION_JUNK_VALUE = "junk_value"
CONVENTIONS = ("notation", "encoding", "normalisation", "renaming", "unfolding", "bundling")
#: The conventions a run of judgment rules 1 was gated by, ``junk_value`` among them.
LEGACY_CONVENTIONS = (*CONVENTIONS, CONVENTION_JUNK_VALUE)
#: What a worked example's absence tags the query for the general theorem it instantiates.
GENERAL_QUERY_TAG = "general"
#: The passage kind whose absence also searched for that general theorem.
PASSAGE_EXAMPLE = "example"
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

# --- stage 9a: the sourceset and the renderer -----------------------------
#: The renderer every web source of the first corpus is rendered by, and a pin of
#: every source entry, plan, gate configuration and record. The id is frozen: a
#: change to any rule that can move one character of any view — the label rule of
#: :func:`is_bold_label` included — is a new id, never an edit of this one.
RENDERER_WEB_HTML = "web-html/3"

#: Where a sourceset entry came from: ``enumerated`` — a listing of the site, at
#: the page's latest revision; ``adopted`` — a revision and bytes a consumer pinned,
#: recorded in the entry's ``adopted_from``, planned instead of an enumerated
#: revision of the same page.
SOURCE_ORIGIN_ENUMERATED = "enumerated"
SOURCE_ORIGIN_ADOPTED = "adopted"
SOURCE_ORIGINS = frozenset({SOURCE_ORIGIN_ENUMERATED, SOURCE_ORIGIN_ADOPTED})

#: What a sourceset entry's ``flags`` say of its page, each true, false, or null for
#: not determined. A page with any of them true is no concept article.
PAGE_FLAG_REDIRECT = "redirect"
PAGE_FLAG_DISAMBIGUATION = "disambiguation"
PAGE_FLAG_LIST = "list"
PAGE_FLAG_BIOGRAPHY = "biography"
PAGE_FLAGS = frozenset({PAGE_FLAG_REDIRECT, PAGE_FLAG_DISAMBIGUATION, PAGE_FLAG_LIST,
                        PAGE_FLAG_BIOGRAPHY})

#: Why a plan left a source out: no layer yet (a render settles it), or the page is
#: no concept article (one of :data:`PAGE_FLAGS` is true).
SKIP_NOT_RENDERED = "not rendered"
SKIP_NO_CONCEPT = "no concept article"
SKIP_REASONS = frozenset({SKIP_NOT_RENDERED, SKIP_NO_CONCEPT})

#: Which finder proposed an inventory passage. The three deterministic finders'
#: passages are pre-filled in an inventory task and dropping one needs a skip naming
#: its span (gate rule 8); ``model`` is the enumerator's own.
FINDER_THEOREM_BOX = "theorem_box"
FINDER_BOLD_DEFINITION = "bold_definition"
FINDER_LEAD = "lead"
FINDER_MODEL = "model"
DETERMINISTIC_FINDERS = frozenset({FINDER_THEOREM_BOX, FINDER_BOLD_DEFINITION, FINDER_LEAD})
FINDERS = DETERMINISTIC_FINDERS | {FINDER_MODEL}

#: The words a bold run that only LABELS a paragraph is made of ("Proof.",
#: "Example 2:", "Definition:"): such a bold names no term, so it makes no
#: ``bold_definition`` passage. These are labels however they are written …
BOLD_LABELS = frozenset({
    "proof", "proofs", "proof sketch", "sketch of proof", "proof idea", "example", "examples",
    "counterexample", "counterexamples", "non-example", "definition", "definitions",
    "theorem", "theorems", "lemma", "lemmas", "corollary", "corollaries", "proposition",
    "propositions", "remark", "remarks", "notation", "exercise", "exercises", "claim",
    "conjecture", "case", "cases", "step", "base case", "inductive step", "induction step",
    "hint", "note", "notes", "warning", "caution"})
#: … and these only with a label's punctuation ("Solution:", "Problem 3."), since
#: without it they may be the term a sentence defines — an enumerator alone is no
#: such mark: "Axiom A", "Problem P", "property (T)" name concepts.
BOLD_LABELS_PUNCTUATED = frozenset({
    "solution", "answer", "problem", "question", "input", "output", "statement",
    "observation", "axiom", "axioms", "fact", "result", "construction", "algorithm",
    "property", "properties", "assumption", "assumptions", "special case", "idea"})
_LABEL_TRIM = " \t.:;,!?–—-"
#: an enumerator a label may carry after its word: 2, 2.1, 3a, iv, (b), (2)
_LABEL_ENUMERATOR = re.compile(r"(?:\s+(?:\(?(?:\d+(?:\.\d+)*[a-z]?|[ivxlc]+|[a-z])\)?))+$")


def is_bold_label(text) -> bool:
    """Is a bold run's text only a paragraph label, in :data:`BOLD_LABELS`?

    Case-folded, with its punctuation stripped and an enumerator after the label word
    dropped ("Case 2:", "Step 1a.", "Example (iii)"); a word of
    :data:`BOLD_LABELS_PUNCTUATED` counts only when a label's punctuation was there
    ("Solution:", "Problem 3."), never for an enumerator alone, which is how a concept
    is named ("Axiom A", "property (T)"); and a proof of something ("Proof of the
    lemma:", "Proof (sketch).") is a label too. A label word followed by a
    parenthesised NAME ("Theorem (Widget lemma).") is no label: it names a statement,
    and its paragraph is a passage.

    The renderer's finders read this rule, so any change to it, or to the two label
    sets, is a new :data:`RENDERER_WEB_HTML` id.
    """
    spaced = " ".join(str(text or "").split()).casefold()
    bare = spaced.strip(_LABEL_TRIM)
    punctuated = bare != spaced
    core = _LABEL_ENUMERATOR.sub("", bare).strip(_LABEL_TRIM)
    if core in BOLD_LABELS or (punctuated and core in BOLD_LABELS_PUNCTUATED):
        return True
    return core.startswith("proof of ") or core.startswith("proof (")
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


def reads_bundles(status_rule: int) -> bool:
    """Whether a run of ``status_rule`` reads its coverage claim as a bundle (2 and on).

    Status rule 3 writes status rule 2's shapes — the bundle, the outcome row, the
    challenge record and the ``/2`` set — and differs only in the statuses it derives
    and in :func:`ranks_by_strength`.
    """
    return status_rule >= STATUS_RULE_BUNDLE


def ranks_by_strength(status_rule: int) -> bool:
    """Whether a run of ``status_rule`` re-ranks a challenge's surviving cards by strength.

    R-SPECIFIC binds a run derived under status rule 3 and no older one: a run judged
    before it ranked "best first" by its worker's own judgement, and a re-tally judges
    an older run by the rule it ran under.
    """
    return status_rule >= STATUS_RULE_BROAD


def status_rule_problem(status_rule) -> str | None:
    """Why ``status_rule`` names no derivation this pipeline knows, or None."""
    if type(status_rule) is not int or status_rule not in STATUS_RULES:
        return f"status rule {status_rule!r} is not one of {list(STATUS_RULES)}"
    return None
