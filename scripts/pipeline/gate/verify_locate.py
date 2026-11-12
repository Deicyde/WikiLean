#!/usr/bin/env python3
"""Stage 7: the out-of-tree locate gate — rules 1 to 10 over one task's own documents.

docs/PIPELINE.md section 3 (stage 7). A worker writes its documents into a worktree and
the gate says whether they may merge. Three things make it a gate rather than a check:

* **out of tree** — it is staged into a gate directory with its ``gate.json``, the task
  manifests, :mod:`locate_text`, :mod:`anchor_resolve` and the record builder, and run by
  absolute path, never out of the worktree a worker can edit;
* **its own documents only** — the documents this contribution changed
  (``--changed-files``), the files ``--findings`` names, or a directory of them, and only
  the sources, candidates and manifests those documents name;
* **the exit vocabulary** — ``0`` every document validated, ``1`` REJECTED (fix the
  documents), ``3`` the gate could not run. Every infrastructure failure arrives as 3 and
  never as the interpreter's 1 out of a traceback, because the worker's prompt says 1
  means "your documents are wrong"; an exit 1 always comes with a ``VALIDATION FAILED``
  list. ``--self-check`` validates nothing and says whether the gate can run.

**The rules.** Each is numbered as the worker's prompt numbers them, and every refusal
names its rule:

1. the schema of the document for its mode with its header pins, and every record the
   document implies built and checked (:mod:`records`, and the vendored Literature Links
   checker where it is importable); a coverage claim sits on a located passage and names
   at least two of its ``hasPart`` locations with a note of the worker's own;
2. every quote matches exactly once on the source's layer, at its passage's span start
   and inside the span; its ``In-text #<hex>`` is the hash of that start's head (an
   inventory's ids recomputed over all its passages at once, a locate or challenge
   document's passages exactly the task's frozen ones); a passage without ``until`` ends
   where the run's ``span_end_rule`` ends it; and the document's ``source_sha``,
   ``layer_sha256`` and ``renderer`` are the pinned source's, whose files hash to the pin;
3. every declaration a document names as found is in the declaration index at that
   library and the universe's commit, and ``proof_status``, ``commit`` and ``version`` are
   refused wherever a worker writes them;
4. with the candidate hits the run pinned for the article: an inventory keeps a
   ``subject`` passage scoped ``formal_math``; a locate document leaves no curated hit
   handed to its task unhandled — located with a relation that counts, or rejected with a
   reason in its receipt. Off in the blind arm;
5. an absence lists at least ``min_absence_queries`` distinct queries, and the document
   names the frozen universe;
6. at most ``max_locations`` locations per passage, with distinct ranks 1..N, one per
   declaration;
7. the changed files are the task's own: ``<findings-dir>/<task-id>/<source-id>.json``
   for the sources its manifest names, and nothing else;
8. an inventory drops no deterministic-finder passage of its task without a skip covering
   its span, every ``math`` paragraph is covered by a passage or a skip, and no skip holds
   the whole span of a passage the inventory keeps;
9. the judgment rules, for a run whose ``gate.json`` names ``judgment_rules``: a counting
   location names the clause ids it states and a ``relatedMatch`` names none; a
   ``closeMatch`` names its convention from the closed list and no other relation carries
   one; a ``hasPart`` or ``narrowMatch`` says what the declaration alone entails; a strong
   location writes its two-way ``tests`` and its ``instantiation``; an absence's receipt
   tags every query with the claims it targets and gives each claim a query of its own; a
   challenge verdict carries what its action owes; and, at rules 5, **the location a
   challenge verdict leaves standing is held to every one of those relation rules**
   (:func:`_verdict_location`, issue #175), the missing-clause check included, and to
   the three checks of :func:`rule9_checks` — the facts the passage asserts and which
   of them the declaration states, a ``broadMatch``'s direction, and the passage's kind
   — and the ``bridge`` of a ``relatedMatch`` is the lemma the prompt promises the gate
   looks up in the index (:func:`rule9_bridge`);
10. the statement cut, for a run whose ``gate.json`` names ``statement_cut_rule``: the
    spans of an inventory are disjoint, one text one passage; no span ends inside a
    formula; a span runs past its block only within the bound the rule allows;
    ``conjuncts`` are none or 2 to 5 with a ``shape`` of the closed list, on a
    ``formal_math`` passage; a passage scoped out is ``supporting``; at most one
    ``formal_math`` passage of a source is ``prominence: subject``; and, at rule 4, no
    passage starts inside a sentence but where a rule of the cut starts one
    (:func:`rule10_sentences`, issue #172).

Ported from formal-companion's ``gate/verify_locate.py``. What changes in the move: the
text layer is :mod:`locate_text`, which is a use of ``site/anchor_resolve``'s whitespace
rule rather than a second copy of it; the declaration index is :mod:`index`'s stated
shape rather than a workspace's database; and the pins are a WikiLean ``gate.json``.

**What is not ported, and is TODO** (docs/PIPELINE.md section 10): rule 8's prose floor;
rule 9's signature-driven instance-binder check and its naturals-for-integers tripwire,
both of which need the index to carry declaration signatures, and its verbatim-source
check over the reading copy's spellings; rule 10's connective and sentence counts for the
harvest receipt. Each is named in :data:`TODO_CHECKS` and reported by ``--self-check``, so
a run host can see what this gate does not yet refuse.

    python3 <gate dir>/verify_locate.py --gate-config <gate dir>/gate.json \\
        [--findings <file> ... | --changed-files <file> | --findings-dir <dir>] \\
        [--task-id <id>] [--pre-merge] [--self-check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
from typing import Any, Iterable

HERE = pathlib.Path(__file__).resolve().parent
for _candidate in (HERE, HERE.parent, HERE.parents[2] / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

#: What went wrong importing the gate's siblings. A missing or broken one is an
#: infrastructure failure: :func:`main` turns it into exit 3 with one FAIL line, never
#: the interpreter's exit 1 out of a traceback.
IMPORT_ERRORS: list[str] = []
try:
    import locate_text as lt
except Exception as _exc:  # noqa: BLE001 — any failure to import is the same failure
    lt = None  # type: ignore[assignment]
    IMPORT_ERRORS.append(f"locate_text.py ({_exc.__class__.__name__}: {_exc})")
try:
    import vocab
except Exception as _exc:  # noqa: BLE001
    vocab = None  # type: ignore[assignment]
    IMPORT_ERRORS.append(f"vocab.py ({_exc.__class__.__name__}: {_exc})")

GATE_SCHEMA = "wikilean-gate-locate/1"
#: The exit codes, which are the gate's contract with the worker and with tend.
EXIT_OK, EXIT_REJECTED, EXIT_BROKEN = 0, 1, 3
#: The checks of the original gate this port does not make yet, named so ``--self-check``
#: can report them rather than letting a host believe they ran.
TODO_CHECKS = (
    "rule 8: the prose floor (every block outside the back matter of at least N words)",
    "rule 9: the instance-binder and naturals-for-integers checks, which read the "
    "declaration's signature from the index",
    "rule 9: free text checked against the reading copy's spellings as well as the layer",
    "rule 10: the sentence, display and connective counts the harvest receipt carries",
)
#: The phrases by which a strong record's note admits a clause it does not state. A
#: strong relation states the whole passage, so a note that says it leaves something out
#: is the record refuting itself.
CLAUSE_GAP_PHRASES = ("does not state", "do not state", "not stated", "not part of",
                      "not a part of", "rather than asserting", "rather than stating",
                      "rather than proving", "only the existence", "only existence",
                      "only proves", "only states", "proves only", "states only",
                      "only one direction", "one direction only", "not the converse",
                      "converse is not", "without the converse", "without uniqueness",
                      "uniqueness is not", "does not cover", "do not cover", "not covered",
                      "not asserted", "not included", "omits", "omitted", "leaves out",
                      "left out", "does not prove", "does not assert", "does not establish")
#: The words an ``exactMatch`` or ``closeMatch`` note may not say: each describes
#: generality or strength, which is a ``broadMatch``, a ``narrowMatch`` or a weakening,
#: never a convention. A trailing ``*`` matches any ending of its last word.
GENERALITY_PHRASES = ("stronger", "strengthen*", "weaker", "weaken*", "more general*",
                      "generali*", "special case", "particular case", "broader", "narrower")


class GateBroken(Exception):
    """The gate cannot run: exit 3, never a rejection of the documents."""


# ---- small readers ----------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: pathlib.Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateBroken(f"{what} ({path}) is not readable JSON: {exc}") from None


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _one_line(value: Any) -> bool:
    return _nonempty(value) and "\n" not in value


def _span(value: Any, length: int) -> tuple[int, int] | None:
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(offset) is int for offset in value)
            and 0 <= value[0] < value[1] <= length):
        return int(value[0]), int(value[1])
    return None


# ---- the configuration -------------------------------------------------------------------


class GateConfig:
    """``gate.json``: everything the gate is pinned to, read once and refused by name."""

    def __init__(self, data: Any, path: pathlib.Path):
        if not isinstance(data, dict):
            raise GateBroken(f"{path} is not a JSON object")
        if data.get("schema") != GATE_SCHEMA:
            raise GateBroken(f"{path} is not a {GATE_SCHEMA} document "
                             f"(schema {data.get('schema')!r})")
        self.path = path
        self.data = data
        self.run_id = str(data.get("run_id") or "")
        self.mode = str(data.get("mode") or "")
        if not self.run_id or self.mode not in (vocab.MODE_INVENTORY, vocab.MODE_LOCATE,
                                                vocab.MODE_CHALLENGE):
            raise GateBroken(f"{path} names run {self.run_id!r} in mode {self.mode!r}; the "
                             f"mode is one of {vocab.MODE_INVENTORY}, {vocab.MODE_LOCATE}, "
                             f"{vocab.MODE_CHALLENGE}")
        universe = data.get("universe")
        if not isinstance(universe, dict) or not isinstance(universe.get("libraries"), dict):
            raise GateBroken(f"{path} pins no universe with its libraries")
        self.universe_id = str(universe.get("id") or "")
        self.universe_sha256 = str(universe.get("universe_sha256") or "")
        self.libraries = {str(lid): dict(lib or {})
                          for lid, lib in universe["libraries"].items()}
        sources = data.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise GateBroken(f"{path} pins no sources")
        self.sources = {str(sid): dict(pin or {}) for sid, pin in sources.items()}
        self.sources_dir = self._dir("sources_dir", required=True)
        self.tasks_dir = self._dir("tasks_dir", required=False)
        self.candidates_dir = self._dir("candidates_dir", required=False)
        self.candidates_sha256 = {str(k): str(v) for k, v in
                                  (data.get("candidates_sha256") or {}).items()}
        self.findings_dir = str(data.get("findings_dir") or "findings")
        self.log_dir = self._dir("log_dir", required=False)
        self.blind = bool(data.get("blind"))
        self.max_locations = self._count("max_locations", 5)
        self.min_absence_queries = self._count("min_absence_queries", 3)
        self.span_end_rule = self._rule("span_end_rule", lt.SPAN_END_RULES, 1)
        self.statement_cut_rule = self._rule("statement_cut_rule", (0, 1, 2, 3, 4), 0)
        self.judgment_rules = self._rule("judgment_rules", (0, 1, 2, 3, 4, 5), 0)
        self.inventory_run = str((data.get("inventory") or {}).get("run_id") or "")
        self.challenged_runs = [str(r) for r in data.get("challenged_runs") or []]
        pin = data.get("index")
        self.index_path = self.resolve(str(pin["path"])) if isinstance(pin, dict) \
            and pin.get("path") else None
        self.index_sha256 = str((pin or {}).get("sha256") or "") if isinstance(pin, dict) else ""

    def _dir(self, key: str, *, required: bool) -> pathlib.Path | None:
        value = self.data.get(key)
        if value is None:
            if required:
                raise GateBroken(f"{self.path} names no {key}")
            return None
        if not isinstance(value, str) or not value:
            raise GateBroken(f"{self.path}: {key} is a directory path, not {value!r}")
        return self.resolve(value)

    def resolve(self, value: str) -> pathlib.Path:
        """A path of ``gate.json``, relative ones taken from its own directory.

        A planned root names everything inside it relatively, so the whole root — gate,
        tasks, sources, index and log — moves to another machine unchanged, which is what
        lets a package carry it with no path of the coordinator's in it.
        """
        path = pathlib.Path(value)
        return path if path.is_absolute() else (self.path.parent / path)

    def _count(self, key: str, default: int) -> int:
        value = self.data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GateBroken(f"{self.path}: {key} is a positive integer, not {value!r}")
        return value

    def _rule(self, key: str, known: Iterable[int], default: int) -> int:
        value = self.data.get(key, default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or value not in tuple(known):
            raise GateBroken(f"{self.path}: {key} is {value!r}, and this gate knows "
                             f"{', '.join(map(str, known))}")
        return value

    def source_pin(self, source_id: str) -> dict:
        pin = self.sources.get(source_id)
        if pin is None:
            raise GateBroken(f"source {source_id!r} is not one run {self.run_id} pinned")
        return pin


# ---- the pinned source's rendered views ----------------------------------------------------


class SourceView:
    """One source's layer, formulas and paragraphs, each held to the gate's pin."""

    def __init__(self, cfg: GateConfig, source_id: str):
        self.source_id = source_id
        self.pin = cfg.source_pin(source_id)
        directory = cfg.sources_dir / source_id
        if not directory.is_dir():
            raise GateBroken(f"no rendered source for {source_id} at {directory}")
        self.dir = directory
        self.layer = self._text("layer.txt")
        got = sha256_bytes(self.layer.encode("utf-8"))
        if self.pin.get("layer_sha256") and got != self.pin["layer_sha256"]:
            raise GateBroken(f"{source_id}: layer.txt hashes to {got[:12]} and the run pinned "
                             f"{str(self.pin['layer_sha256'])[:12]}; a span is an offset into "
                             "one layer, and this is not it")
        views = self.pin.get("views") if isinstance(self.pin.get("views"), dict) else {}
        self.formulas = self._view("formulas.json", views)
        self.paragraphs = self._view("paragraphs.json", views)
        self.finders = self._optional("finders.json")
        self.head_map = lt.HeadMap(self.layer, self.formulas)
        self.blocks = lt.Blocks(self.layer, self.paragraphs, self.formulas)

    def _text(self, name: str) -> str:
        path = self.dir / name
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GateBroken(f"{self.source_id}: {name} cannot be read ({exc})") from None

    def _view(self, name: str, views: dict) -> list:
        path = self.dir / name
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise GateBroken(f"{self.source_id}: {name} is missing ({exc})") from None
        want = views.get(name)
        if want and sha256_bytes(raw) != want:
            raise GateBroken(f"{self.source_id}: {name} is not the view the run pinned")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GateBroken(f"{self.source_id}: {name} is not readable JSON ({exc})") from None
        if not isinstance(value, list):
            raise GateBroken(f"{self.source_id}: {name} is not a list")
        return value

    def _optional(self, name: str) -> list:
        path = self.dir / name
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return value if isinstance(value, list) else []


# ---- the run's context --------------------------------------------------------------------


class Context:
    """What the rules read beside a document: the sources, the index and the candidates."""

    def __init__(self, cfg: GateConfig):
        self.cfg = cfg
        self._views: dict[str, SourceView] = {}
        self._candidates: dict[str, dict] = {}
        self.index = None
        self.index_problem: str | None = None
        if cfg.index_path is not None:
            self._open_index()

    def _open_index(self) -> None:
        cfg = self.cfg
        path = cfg.index_path
        if path is None or not path.is_file():
            raise GateBroken(f"no declaration index at {path}")
        if cfg.index_sha256:
            got = sha256_bytes(path.read_bytes())
            if got != cfg.index_sha256:
                raise GateBroken(f"the declaration index at {path} hashes to {got[:12]} and the "
                                 f"run pinned {cfg.index_sha256[:12]}")
        try:
            import index as index_mod  # noqa: PLC0415 — staged beside the gate
        except Exception as exc:  # noqa: BLE001
            raise GateBroken(f"the declaration index reader is missing from the gate directory "
                             f"({exc})") from None
        try:
            self.index = index_mod.open_index(path)
        except Exception as exc:  # noqa: BLE001
            raise GateBroken(f"the declaration index cannot be read ({exc})") from None

    def view(self, source_id: str) -> SourceView:
        if source_id not in self._views:
            self._views[source_id] = SourceView(self.cfg, source_id)
        return self._views[source_id]

    def candidates(self, source_id: str) -> dict:
        """The candidates file the run pinned for a source, or ``{}`` in the blind arm."""
        if self.cfg.blind or self.cfg.candidates_dir is None:
            return {}
        if source_id not in self._candidates:
            path = self.cfg.candidates_dir / f"{source_id}.json"
            if not path.is_file():
                raise GateBroken(f"no candidates file for {source_id} at {path}")
            raw = path.read_bytes()
            want = self.cfg.candidates_sha256.get(source_id)
            if want and sha256_bytes(raw) != want:
                raise GateBroken(f"the candidates file for {source_id} is not the bytes the run "
                                 "pinned")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise GateBroken(f"the candidates file for {source_id} is not JSON "
                                 f"({exc})") from None
            self._candidates[source_id] = value if isinstance(value, dict) else {}
        return self._candidates[source_id]

    def curated(self, source_id: str) -> list[tuple[str, str]]:
        """``[(library, declaration)]``: the curated hits the run handed the task."""
        rows = self.candidates(source_id).get("curated")
        out = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("library") and row.get("declaration"):
                out.append((str(row["library"]), str(row["declaration"])))
        return out

    def task_curated(self, task_id: str, source_id: str) -> list[tuple[str, str]] | None:
        """The curated hits this task's manifest handed it for a source, or None.

        None when there is no manifest to read: the caller then falls back to the whole
        source's pinned hits, which is the stricter reading of "handed to the task".
        """
        manifest = self.task_manifest(task_id)
        if manifest is None:
            return None
        for document in manifest.get("documents") or []:
            if str(document.get("source_id")) != source_id:
                continue
            out = []
            for row in document.get("curated") or []:
                if isinstance(row, dict) and row.get("library") and row.get("declaration"):
                    out.append((str(row["library"]), str(row["declaration"])))
            return out
        return []

    def task_manifest(self, task_id: str) -> dict | None:
        if self.cfg.tasks_dir is None or not task_id:
            return None
        path = self.cfg.tasks_dir / f"{task_id}.json"
        if not path.is_file():
            return None
        value = read_json(path, f"the manifest of task {task_id}")
        return value if isinstance(value, dict) else None


# ---- the problem list -----------------------------------------------------------------------


class Problems:
    """Every refusal, each naming its rule and the document it is of."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, str]] = []

    def add(self, rule: int, where: str, message: str) -> None:
        self.rows.append((rule, where, message))

    def __bool__(self) -> bool:
        return bool(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def lines(self) -> list[str]:
        return [f"rule {rule}: {where}: {message}" for rule, where, message in self.rows]

    def rules(self) -> list[int]:
        return sorted({rule for rule, _where, _message in self.rows})


# ---- rule 1: the schema and the records ---------------------------------------------------

#: The header keys every harvested document carries.
HEADER_KEYS = ("schema", "mode", "run_id", "task_id", "source_id", "source_sha",
               "layer_sha256", "renderer")


def rule1_header(doc: dict, where: str, cfg: GateConfig, problems: Problems,
                 task_id: str) -> str | None:
    """The document's header: its schema, its mode, its run and its source's pins."""
    if doc.get("schema") != vocab.DOCUMENT_SCHEMA:
        problems.add(1, where, f"a document's schema is {vocab.DOCUMENT_SCHEMA}, not "
                               f"{doc.get('schema')!r}")
    if doc.get("mode") != cfg.mode:
        problems.add(1, where, f"run {cfg.run_id} is a {cfg.mode} run and this document says "
                               f"mode {doc.get('mode')!r}")
    if doc.get("run_id") != cfg.run_id:
        problems.add(1, where, f"the document names run {doc.get('run_id')!r}, not {cfg.run_id}")
    if task_id and doc.get("task_id") != task_id:
        problems.add(1, where, f"the document names task {doc.get('task_id')!r} and lives in "
                               f"{task_id}")
    source_id = doc.get("source_id")
    if not _nonempty(source_id) or source_id not in cfg.sources:
        problems.add(1, where, f"source_id {source_id!r} is not one run {cfg.run_id} pinned")
        return None
    pin = cfg.sources[str(source_id)]
    for key, pinned in (("source_sha", pin.get("html_sha256")),
                        ("layer_sha256", pin.get("layer_sha256")),
                        ("renderer", pin.get("renderer"))):
        if pinned and doc.get(key) != pinned:
            problems.add(1, where, f"{key} is {str(doc.get(key))[:12]!r} and the run pinned "
                                   f"{str(pinned)[:12]!r} for {source_id}")
    return str(source_id)


def rule1_records(doc: dict, where: str, cfg: GateConfig, ctx: Context,
                  problems: Problems) -> None:
    """Every location the document writes, joined with the index and held to the record
    builder's own shape check.

    ``records.location_problem`` is the check the export and the store builder run, so a
    document the gate passes here is one whose records the export can write: the gate
    refuses the record the export would refuse, rather than a paraphrase of it. The join
    is the gate's, not the worker's — a worker writes no ``proof_status`` (rule 3) — so
    the index row is put beside the worker's fields before the check.
    """
    if doc.get("mode") != vocab.MODE_LOCATE:
        return
    try:
        import records as records_mod  # noqa: PLC0415 — staged beside the gate
    except Exception as exc:  # noqa: BLE001
        raise GateBroken(f"the record builder is missing from the gate directory ({exc})") \
            from None
    for number, passage in enumerate(doc.get("passages") or []):
        item = str(passage.get("item") or f"passages[{number}]")
        for location in passage.get("locations") or []:
            if not isinstance(location, dict):
                continue
            joined = dict(location)
            row = None
            if ctx.index is not None:
                commit = str(cfg.libraries.get(str(location.get("library")), {})
                             .get("commit") or "")
                row = ctx.index.declaration(str(location.get("library")),
                                            str(location.get("declaration")), commit)
            if row is None:
                # rule 3 names the missing declaration; the record cannot be built without
                # its index row, and nothing is gained by naming the same gap twice
                continue
            joined.update({key: row.get(key) for key in
                           ("proof_status", "statement", "module", "path", "line", "end_line")})
            problem = records_mod.location_problem(joined)
            if problem:
                problems.add(1, where, f"{item}: {problem}")


def rule1_coverage(passage: dict, where: str, item: str, problems: Problems) -> None:
    """A coverage claim sits on a located passage, names at least two of its ``hasPart``
    locations and says in a note of the worker's own how the parts state every clause."""
    claim = passage.get("coverage")
    if claim is None:
        return
    if passage.get("outcome") != vocab.OUTCOME_LOCATED:
        problems.add(1, where, f"{item}: a coverage claim sits on a located passage, not an "
                               f"{passage.get('outcome')!r} one")
        return
    if not isinstance(claim, dict) or claim.get("complete") is not True:
        problems.add(1, where, f"{item}: a coverage claim says complete: true")
        return
    ranks = claim.get("parts")
    if not isinstance(ranks, list) or not all(type(rank) is int for rank in ranks) \
            or len(set(ranks)) != len(ranks) or len(ranks) < vocab.MIN_COVERAGE_PARTS:
        problems.add(1, where, f"{item}: a coverage claim names at least "
                               f"{vocab.MIN_COVERAGE_PARTS} distinct ranks, not {ranks!r}")
        return
    by_rank = {loc.get("rank"): loc for loc in passage.get("locations") or []}
    for rank in ranks:
        loc = by_rank.get(rank)
        if loc is None or loc.get("relation") != vocab.HAS_PART:
            problems.add(1, where, f"{item}: coverage part {rank} is not the rank of one of the "
                                   "passage's hasPart locations")
    if not _nonempty(claim.get("note")):
        problems.add(1, where, f"{item}: a coverage claim says in its note how the parts state "
                               "every clause")


# ---- rule 2: the quote, the id and the span ------------------------------------------------


def rule2_passages(doc: dict, where: str, cfg: GateConfig, view: SourceView,
                   problems: Problems, *, frozen: dict | None) -> dict[str, tuple[int, int]]:
    """Place every passage on the layer and recompute its id and its end.

    Returns ``{item: span}`` of the passages that were placed, which rules 8 and 10 read.
    An inventory's ids are recomputed over **all** of its passages at once, because the
    head rule makes an id depend on the others; a locate or challenge document's passages
    must be exactly the frozen ones its task was handed, so their ids are checked against
    the inventory rather than re-derived.
    """
    placed: dict[str, tuple[int, int]] = {}
    rows: list[tuple[str, int, int]] = []       # (item, layer start, head start)
    for number, passage in enumerate(doc.get("passages") or []):
        item = str(passage.get("item") or f"passages[{number}]")
        quote = passage.get("quote") if isinstance(passage.get("quote"), dict) else None
        if frozen is not None:
            base = frozen.get(item)
            if base is None:
                problems.add(2, where, f"{item} is no passage of this task's frozen inventory")
                continue
            span = _span(base.get("span"), len(view.layer))
            if span is None:
                problems.add(2, where, f"{item}: the frozen inventory gives it no span")
                continue
            placed[item] = span
            continue
        if quote is None or not _nonempty(quote.get("exact")):
            problems.add(2, where, f"{item}: a passage carries the quote it was cut at")
            continue
        hits = lt.find_quote(view.layer, quote.get("exact"), quote.get("prefix") or "",
                             quote.get("suffix") or "")
        if len(hits) != 1:
            problems.add(2, where, f"{item}: the quote occurs {len(hits)} times on the layer; "
                                   "it must occur exactly once with its prefix and suffix")
            continue
        start = hits[0]
        span = _span(passage.get("span"), len(view.layer))
        if span is None:
            problems.add(2, where, f"{item}: span {passage.get('span')!r} is no [start, end] "
                                   f"inside the layer (0..{len(view.layer)})")
            continue
        if span[0] != start:
            problems.add(2, where, f"{item}: the quote is at {start} and the span starts at "
                                   f"{span[0]}")
            continue
        if start + len(quote["exact"]) > span[1]:
            problems.add(2, where, f"{item}: the quote runs past the end of the span")
            continue
        try:
            end = lt.passage_end(view.layer, view.blocks, start, quote["exact"],
                                 passage.get("until"), rule=cfg.span_end_rule,
                                 next_start=None, formulas=view.formulas)
        except (lt.HeadError, ValueError) as exc:
            problems.add(2, where, f"{item}: {exc}")
            continue
        if passage.get("until") and end != span[1]:
            problems.add(2, where, f"{item}: `until` ends the passage at {end} and the span "
                                   f"ends at {span[1]}")
            continue
        try:
            head_start = lt.check_start(view.head_map.head, view.head_map.to_head(start))
        except lt.HeadError as exc:
            problems.add(2, where, f"{item}: {exc}")
            continue
        placed[item] = span
        rows.append((item, start, head_start))
    if frozen is None and rows:
        _rule2_ids(doc, where, view, rows, problems, cfg)
    return placed


def _rule2_ids(doc: dict, where: str, view: SourceView, rows: list[tuple[str, int, int]],
               problems: Problems, cfg: GateConfig) -> None:
    """Recompute every id of the document together, and the end of a passage with no
    ``until`` under the run's span-end rule (which needs every start)."""
    try:
        ids = lt.assign_ids(view.head_map.head, [head for _item, _start, head in rows])
    except lt.HeadError as exc:
        problems.add(2, where, str(exc))
        return
    for (item, start, _head), (_at, digest, words) in zip(rows, ids):
        want = "In-text #" + digest
        if item != want:
            problems.add(2, where, f"{item}: the head at {start} hashes to {want}")
        passage = next((p for p in doc.get("passages") or [] if p.get("item") == item), None)
        if passage is not None and passage.get("head_words") not in (None, words):
            problems.add(2, where, f"{item}: head_words is {passage.get('head_words')!r} and "
                                   f"the head rule gives {words}")
    if cfg.span_end_rule == 1:
        return
    starts = sorted({start for _item, start, _head in rows})
    for item, start, _head in rows:
        passage = next((p for p in doc.get("passages") or [] if p.get("item") == item), None)
        if passage is None or passage.get("until"):
            continue
        after = [s for s in starts if s > start]
        try:
            end = lt.passage_end(view.layer, view.blocks, start,
                                 (passage.get("quote") or {}).get("exact") or "", None,
                                 rule=cfg.span_end_rule,
                                 next_start=after[0] if after else None,
                                 formulas=view.formulas)
        except (lt.HeadError, ValueError) as exc:
            problems.add(2, where, f"{item}: {exc}")
            continue
        if (passage.get("span") or [None, None])[1] != end:
            problems.add(2, where, f"{item}: span-end rule {cfg.span_end_rule} ends the passage "
                                   f"at {end} and the span ends at "
                                   f"{(passage.get('span') or [None, None])[1]}")


# ---- rule 3: the declarations ----------------------------------------------------------------

#: What a worker never writes about a declaration: the index decides each of them.
WORKER_MAY_NOT_WRITE = ("proof_status", "commit", "version")


def rule3_declaration(location: dict, where: str, item: str, cfg: GateConfig, ctx: Context,
                      problems: Problems) -> None:
    """The declaration exists in the index at the universe's commit, and the worker wrote
    nothing the index decides."""
    for key in WORKER_MAY_NOT_WRITE:
        if key in location:
            problems.add(3, where, f"{item}: a worker does not write {key}; the declaration "
                                   "index decides it")
    library = str(location.get("library") or "")
    declaration = str(location.get("declaration") or "")
    if library not in cfg.libraries:
        problems.add(3, where, f"{item}: library {library!r} is not in universe "
                               f"{cfg.universe_id}")
        return
    if ctx.index is None:
        return
    commit = str(cfg.libraries[library].get("commit") or "")
    if ctx.index.declaration(library, declaration, commit) is None:
        problems.add(3, where, f"{item}: {library}:{declaration} is not in the declaration "
                               f"index at {commit[:12]}")


# ---- rule 4: the candidates handed to the task ----------------------------------------------


def rule4_locate(doc: dict, where: str, cfg: GateConfig, ctx: Context,
                 problems: Problems, source_id: str, task_id: str) -> None:
    """No curated hit handed to **this task** is left unhandled.

    Each is located with a relation that counts — a ``relatedMatch`` does not — or
    rejected by name with a reason in some passage's receipt. A source's passages are
    split across tasks, so the hits are the ones this task's manifest was given, and each
    is checked against the pinned candidates file: a manifest hit the pin does not carry
    is itself a refusal, since the hits a worker answers must be the ones the run froze.
    Off in the blind arm.
    """
    if cfg.blind:
        return
    pinned = set(ctx.curated(source_id))
    handed = ctx.task_curated(task_id, source_id)
    if handed is None:
        curated = sorted(pinned)
    else:
        curated = sorted(handed)
        for hit in sorted(set(handed) - pinned):
            problems.add(4, where, f"the task was handed the curated candidate "
                                   f"{hit[0]}:{hit[1]}, which the run's pinned candidates "
                                   "file does not carry")
    if not curated:
        return
    handled: set[tuple[str, str]] = set()
    for passage in doc.get("passages") or []:
        for location in passage.get("locations") or []:
            if location.get("relation") != vocab.RELATED_MATCH:
                handled.add((str(location.get("library")), str(location.get("declaration"))))
        receipt = passage.get("receipt") if isinstance(passage.get("receipt"), dict) else {}
        for candidate in receipt.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            key = (str(candidate.get("library")), str(candidate.get("declaration")))
            if _nonempty(candidate.get("reason")):
                handled.add(key)
    for library, declaration in curated:
        if (library, declaration) not in handled:
            problems.add(4, where, f"the curated candidate {library}:{declaration} was handed "
                                   "to this task and is neither located with a relation that "
                                   "counts nor rejected with a reason")


def rule4_inventory(doc: dict, where: str, ctx: Context, problems: Problems,
                    source_id: str, cfg: GateConfig) -> None:
    """An inventory with curated hits keeps a ``subject`` passage scoped ``formal_math``."""
    if cfg.blind or not ctx.curated(source_id):
        return
    for passage in doc.get("passages") or []:
        if passage.get("prominence") == vocab.PROMINENCE_SUBJECT \
                and passage.get("scope") != vocab.SCOPE_FORMAL_MATH:
            problems.add(4, where, f"{passage.get('item')}: the article's subject passage is "
                                   f"scoped {passage.get('scope')!r}, and the run handed this "
                                   "source curated candidates")


# ---- rule 5: an absence's receipt --------------------------------------------------------------


def rule5_absence(doc: dict, where: str, cfg: GateConfig, problems: Problems) -> None:
    """An absence lists at least N distinct queries, and the document names the universe."""
    if doc.get("mode") in (vocab.MODE_LOCATE, vocab.MODE_CHALLENGE):
        if doc.get("universe") != cfg.universe_id or (
                cfg.universe_sha256 and doc.get("universe_sha256") != cfg.universe_sha256):
            problems.add(5, where, f"the document names universe {doc.get('universe')!r} at "
                                   f"{str(doc.get('universe_sha256'))[:12]}, and the run froze "
                                   f"{cfg.universe_id} at {cfg.universe_sha256[:12]}")
    for passage in doc.get("passages") or []:
        if passage.get("outcome") != vocab.OUTCOME_ABSENT:
            continue
        item = str(passage.get("item"))
        receipt = passage.get("receipt") if isinstance(passage.get("receipt"), dict) else None
        queries = (receipt or {}).get("queries")
        distinct = {q.strip() for q in queries or [] if _nonempty(q)}
        if len(distinct) < cfg.min_absence_queries:
            problems.add(5, where, f"{item}: a checked absence lists at least "
                                   f"{cfg.min_absence_queries} distinct queries, not "
                                   f"{len(distinct)}")


# ---- rule 6: the locations of one passage --------------------------------------------------------


def rule6_locations(passage: dict, where: str, item: str, cfg: GateConfig,
                    problems: Problems) -> None:
    """At most N locations, distinct ranks 1..N, one per declaration."""
    locations = passage.get("locations") or []
    if len(locations) > cfg.max_locations:
        problems.add(6, where, f"{item}: {len(locations)} locations, over the cap of "
                               f"{cfg.max_locations}")
    ranks = [loc.get("rank") for loc in locations]
    want = list(range(1, len(locations) + 1))
    if sorted(rank for rank in ranks if type(rank) is int) != want or len(set(ranks)) != len(ranks):
        problems.add(6, where, f"{item}: the ranks are {ranks!r}; a passage's locations are "
                               f"ranked {want!r}, each rank once")
    seen: set[tuple[str, str]] = set()
    for location in locations:
        key = (str(location.get("library")), str(location.get("declaration")))
        if key in seen:
            problems.add(6, where, f"{item}: {key[0]}:{key[1]} is listed twice")
        seen.add(key)


# ---- rule 7: the changed files are the task's own ---------------------------------------------


def rule7_changed_files(paths: list[str], cfg: GateConfig, ctx: Context, problems: Problems,
                        *, task_id: str, pre_merge: bool) -> None:
    """Every changed file is a document of this task, for a source its manifest names."""
    prefix = cfg.findings_dir.strip("/")
    for rel in paths:
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] != prefix or not parts[2].endswith(".json"):
            problems.add(7, rel, f"a task writes {prefix}/<task-id>/<source-id>.json and "
                                 "nothing else")
            continue
        owner, name = parts[1], parts[2][:-len(".json")]
        if not pre_merge and task_id and owner != task_id:
            problems.add(7, rel, f"this is task {task_id}'s call and the file belongs to "
                                 f"{owner}")
            continue
        manifest = ctx.task_manifest(owner)
        if manifest is None:
            if cfg.tasks_dir is not None:
                problems.add(7, rel, f"there is no manifest for task {owner}")
            continue
        wanted = {str(d.get("source_id")) for d in manifest.get("documents") or []}
        if name not in wanted:
            problems.add(7, rel, f"task {owner} was handed "
                                 f"{', '.join(sorted(wanted)) or 'no source'}, not {name}")


# ---- rule 8: an inventory covers what it was handed ------------------------------------------


def rule8_coverage(doc: dict, where: str, view: SourceView, placed: dict,
                   problems: Problems) -> None:
    """Every finder passage and every ``math`` paragraph is covered by a passage or a skip,
    and no skip holds the whole span of a passage the inventory keeps.

    TODO (``TODO_CHECKS``): the prose floor, which also asks a block of prose outside the
    back matter to be covered.
    """
    spans = sorted(placed.values())
    skips: list[tuple[int, int]] = []
    for number, skip in enumerate(doc.get("skips") or []):
        span = _span((skip or {}).get("span"), len(view.layer)) if isinstance(skip, dict) \
            else None
        if span is None:
            problems.add(8, where, f"skips[{number}] has no [start, end] span inside the layer")
            continue
        if not _nonempty(skip.get("reason")):
            problems.add(8, where, f"skips[{number}] gives no reason")
        skips.append(span)
    for a, b in skips:
        for item, (pa, pb) in placed.items():
            if a <= pa and pb <= b:
                problems.add(8, where, f"a skip [{a}, {b}] holds the whole span of {item}, "
                                       "which the inventory keeps")
    def covered(a: int, b: int) -> bool:
        return any(sa <= a and b <= sb for sa, sb in spans) or \
            any(sa <= a and b <= sb for sa, sb in skips) or \
            any(sa < b and a < sb for sa, sb in spans)

    for finder in view.finders:
        span = _span((finder or {}).get("span"), len(view.layer)) if isinstance(finder, dict) \
            else None
        if span is None or not _nonempty((finder or {}).get("finder")):
            continue
        if not covered(*span):
            problems.add(8, where, f"the {finder['finder']} finder's passage at {list(span)} is "
                                   "neither kept nor covered by a skip")
    for para in view.paragraphs:
        if not isinstance(para, dict) or not para.get("math"):
            continue
        span = _span(para.get("span"), len(view.layer))
        if span is None:
            continue
        if not covered(*span):
            problems.add(8, where, f"the math paragraph at {list(span)} is neither covered by a "
                                   "passage nor by a skip")


# ---- rule 9: the judgment rules -----------------------------------------------------------------


def _clause_ids(frozen_passage: dict | None) -> list[str]:
    """The clause ids a location may name: the frozen passage's conjuncts, else ``c1``."""
    conjuncts = (frozen_passage or {}).get("conjuncts")
    if isinstance(conjuncts, list) and conjuncts:
        return [str(c.get("id")) for c in conjuncts if isinstance(c, dict)]
    return [vocab.SIMPLE_STATEMENT_CLAUSE]


def fold_words(text: Any) -> str:
    """``text`` as the phrase lists read it: case folded, contractions spelled out."""
    folded = str(text or "").replace("’", "'").casefold()
    folded = re.sub(r"(?<![\w'])can't(?![\w'])", "can not", folded)
    folded = re.sub(r"(?<![\w'])won't(?![\w'])", "will not", folded)
    folded = re.sub(r"(?<![\w'])cannot(?![\w'])", "can not", folded)
    folded = re.sub(r"(?<=\w)n't(?![\w'])", " not", folded)
    return " ".join(folded.split())


def phrase_pattern(phrase: str) -> re.Pattern:
    """A phrase of a list as a pattern; a trailing ``*`` matches any ending."""
    wild = phrase.endswith("*")
    body = r"\s".join(re.escape(word) for word in phrase.rstrip("*").split())
    return re.compile(rf"(?<![\w']){body}" + (r"[\w']*" if wild else r"(?![\w'])"))


_CLAUSE_END = re.compile(r"[.;:!?,()\[\]]")
_PASSAGE_WORD = re.compile(r"(?<![\w'])(?:passage|article)(?![\w'])")
_PASSAGE_AFTER = re.compile(r"\s*(?:by |in |of |from |for )?(?:the |this )(?:passage|article)"
                            r"(?![\w'])")
_NEGATED = re.compile(r"(?<![\w'])not\s*\Z")
_RELATIVE = re.compile(r"\s*(?:which|that|whose|where)(?![\w'])")


def _gap_exempt(folded: str, start: int, end: int, phrase: str) -> bool:
    """A gap phrase's hit that admits no gap of the declaration's.

    "not only states" is no gap; neither is a clause about the *passage* ("the passage
    does not state …"), nor a relative clause after "unlike …".
    """
    before, after = folded[:start], folded[end:]
    if phrase.startswith("only") and _NEGATED.search(before):
        return True
    cuts = [m.end() for m in _CLAUSE_END.finditer(before)]
    clause_start = cuts[-1] if cuts else 0
    clause = before[clause_start:]
    stop = _CLAUSE_END.search(after)
    if _PASSAGE_WORD.search(clause) \
            or _PASSAGE_AFTER.match(after[:stop.start() if stop else len(after)]):
        return True
    if cuts and _RELATIVE.match(clause):
        previous = before[cuts[-2] if len(cuts) >= 2 else 0:clause_start - 1]
        if previous.strip().startswith("unlike"):
            return True
    return False


def admits_missing_clause(note: Any) -> str | None:
    """The phrase by which a note admits a clause it does not state, or None."""
    folded = fold_words(note)
    for phrase in CLAUSE_GAP_PHRASES:
        for hit in phrase_pattern(phrase).finditer(folded):
            if not _gap_exempt(folded, hit.start(), hit.end(), phrase):
                return phrase
    return None


def says_generality(note: Any) -> str | None:
    """The generality word an exact or close note may not say, or None."""
    folded = fold_words(note)
    return next((phrase for phrase in GENERALITY_PHRASES
                 if phrase_pattern(phrase).search(folded)), None)


def rule9_location(location: dict, where: str, item: str, cfg: GateConfig,
                   problems: Problems, ids: list[str], *, challenger: bool = False) -> None:
    """What the judgment rules ask of one location the document leaves standing."""
    relation = location.get("relation")
    strong = relation in vocab.STRONG_RELATIONS
    held = relation
    if relation == vocab.HAS_PART and location.get("conjunct_relation") in \
            vocab.CONJUNCT_RELATIONS_PART:
        held = location["conjunct_relation"]
    clauses = location.get("clauses")
    if relation == vocab.RELATED_MATCH:
        if clauses:
            problems.add(9, where, f"{item}: a relatedMatch states no clause and names "
                                   f"{clauses!r}")
    else:
        if not isinstance(clauses, list) or not clauses:
            problems.add(9, where, f"{item}: a {relation} names the clause ids it states")
        else:
            unknown = [c for c in clauses if c not in ids]
            if unknown:
                problems.add(9, where, f"{item}: clause(s) {', '.join(map(str, unknown))} are "
                                       f"not this passage's ({', '.join(ids)})")
            elif strong and sorted(set(clauses)) != sorted(set(ids)):
                problems.add(9, where, f"{item}: a {relation} states every clause of the "
                                       f"passage, and names {sorted(set(clauses))}")
    convention = location.get("convention")
    # under judgment rules 1 ``junk_value`` was a convention; the maintainer's PD7 took it
    # out, and a challenger's confirm that keeps such a record's own still stands
    allowed = vocab.CONVENTIONS if cfg.judgment_rules >= 2 and not challenger \
        else vocab.LEGACY_CONVENTIONS
    if convention is not None:
        if held != vocab.CLOSE_MATCH:
            problems.add(9, where, f"{item}: a convention belongs to a closeMatch, not a "
                                   f"{relation}")
        elif convention not in allowed:
            problems.add(9, where, f"{item}: convention {convention!r} is not one of "
                                   f"{', '.join(allowed)}")
    elif held == vocab.CLOSE_MATCH:
        problems.add(9, where, f"{item}: a closeMatch names the convention it differs by")
    step = location.get("step")
    if step is not None and relation not in vocab.STEP_RELATIONS:
        problems.add(9, where, f"{item}: a step belongs to a hasPart or a narrowMatch, not a "
                               f"{relation}")
    if relation in vocab.STEP_RELATIONS and not _one_line(step):
        problems.add(9, where, f"{item}: a {relation} says in `step` what the declaration alone "
                               "entails of the passage")
    if held in vocab.STRONG_RELATIONS:
        if not _nonempty(location.get("instantiation")):
            problems.add(9, where, f"{item}: a {relation} says how the declaration is "
                                   "instantiated at the passage's objects")
        if cfg.judgment_rules >= 3:
            tests = location.get("tests")
            if not isinstance(tests, dict) or not all(_nonempty(tests.get(key))
                                                      for key in ("r2", "r3")):
                problems.add(9, where, f"{item}: a {relation} runs both tests (r2 and r3)")
    if relation in vocab.STRONG_RELATIONS:
        # the gap check is the location's OWN relation's: a part states some conjuncts and
        # not the rest by definition, and says so in its step
        for field in ("note", "instantiation"):
            phrase = admits_missing_clause(location.get(field))
            if phrase:
                problems.add(9, where, f"{item}: the {field} of a {relation} says {phrase!r}, "
                                       "which admits a clause it does not state; a strong "
                                       "relation states the whole passage")
                break
    if held in (vocab.EXACT_MATCH, vocab.CLOSE_MATCH):
        phrase = says_generality(location.get("note"))
        if phrase:
            problems.add(9, where, f"{item}: the note of a {relation} says {phrase!r}, which "
                                   "describes generality or strength; that is a broadMatch, a "
                                   "narrowMatch or a weakening, never a convention")
    if relation == vocab.HAS_PART and cfg.judgment_rules >= 3:
        given = location.get("conjunct_relation")
        if given is not None and given not in vocab.CONJUNCT_RELATIONS_PART:
            problems.add(9, where, f"{item}: conjunct_relation {given!r} is none of "
                                   f"{', '.join(sorted(vocab.CONJUNCT_RELATIONS_PART))}")


#: What a verdict may say of the location it leaves, beside the action's own fields.
#: They are the judgment fields a locate document writes, so a verdict replaces the
#: record's one at a time and the rest of the record stands.
VERDICT_LOCATION_FIELDS = ("relation", "note", "clauses", "instantiation", "step",
                           "convention", "conjunct_relation", "tests")
#: Which of them the tally takes from a verdict only when it is a non-empty string
#: (``tally._judged_fields``): a blank one leaves the record's own standing, so the gate
#: holds the record's too — otherwise ``"note": ""`` would answer the missing-clause
#: rule with a field the tally throws away.
VERDICT_TEXT_FIELDS = ("note", "instantiation", "step", "convention")


def _written(verdict: dict, key: str) -> bool:
    """Whether the verdict writes ``key`` over the record's, as the tally reads it."""
    value = verdict.get(key)
    if value is None:
        return False
    if key in VERDICT_TEXT_FIELDS:
        return isinstance(value, str) and bool(value.strip())
    return True


def _verdict_location(record: dict, verdict: dict) -> dict:
    """The location a challenge verdict leaves standing (judgment rules 5).

    A challenge document carries no locations of its own: the relation a ``confirm``
    leaves is the **record's**, which the task manifest carries, and a ``downgrade``'s is
    the one it names. Every judgment field the verdict writes replaces the record's, and
    a field the relation it leaves does not take is dropped exactly as the tally drops it
    (``tally._fit_fields``) — so the gate holds the location the tally will write, and
    refuses no convention or clause list the tally itself would throw away. "Writes" is
    the tally's reading of it (:func:`_written`): a blank string is no field, and the
    record's own stands, so a verdict cannot answer a rule about its record's note by
    blanking the note.

    Without this the judgment rules hold of a locate document and of nothing a challenger
    writes: a ``confirm`` owed a non-empty witness and nothing else (issue #175).
    """
    left = {key: record.get(key) for key in VERDICT_LOCATION_FIELDS}
    if verdict.get("action") == "downgrade":
        left["relation"] = verdict.get("relation")
    for key in VERDICT_LOCATION_FIELDS[1:]:
        if _written(verdict, key):
            left[key] = verdict[key]
    left = {key: value for key, value in left.items() if value is not None}
    relation = left.get("relation")
    if relation != vocab.HAS_PART:
        left.pop("conjunct_relation", None)
    if relation != vocab.CLOSE_MATCH \
            and left.get("conjunct_relation") != vocab.CLOSE_MATCH:
        left.pop("convention", None)
    if relation not in vocab.STEP_RELATIONS:
        left.pop("step", None)
    if relation == vocab.RELATED_MATCH and verdict.get("clauses") is None:
        # the record's clauses are the relation it no longer has; a clause list the
        # verdict writes itself is its own claim, and rule 9 holds it to it
        left.pop("clauses", None)
    return left


def _says_none(line: Any) -> bool:
    """Whether a ``direction.hypotheses`` line says there is no such hypothesis.

    Its first word, and nothing else about it: a challenger told to write ``none``
    writes "none", "None." or "none — it assumes nothing the passage does not", and
    refusing the last two would spend a model call on their punctuation. One that names
    a hypothesis begins with the hypothesis.
    """
    first = re.search(r"[a-z]+", str(line or "").casefold())
    return first is not None and first.group(0) == "none"


def rule9_checks(verdict: dict, left: dict, where: str, item: str,
                 problems: Problems) -> None:
    """Judgment rules 5: the clause, direction and kind checks a verdict owes.

    The clause check is the one the inventory's ids cannot make: a passage the inventory
    read as simple has one clause id, so naming it says nothing about the facts the
    passage asserts. ``reading`` is the challenger's own decomposition in its own words
    and ``states`` says which of those facts the declaration itself states — every one of
    them for a strong relation, at least one for a ``hasPart``. ``direction`` is R2's
    instantiation on a ``broadMatch``, with every hypothesis the passage does not have
    (PD4). ``kind_check`` is the notation-or-worked-example reading of the passage.

    A ``checks`` block on a verdict that leaves a weaker relation is extra and not a
    refusal — the tally drops a field the relation does not take, and a refusal here
    would spend a model call removing something that harms nothing.
    """
    relation = left.get("relation")
    if relation not in vocab.CHECKED_RELATIONS:
        return
    held = relation
    if relation == vocab.HAS_PART and left.get("conjunct_relation") in \
            vocab.CONJUNCT_RELATIONS_PART:
        held = left["conjunct_relation"]
    checks = verdict.get(vocab.CHECKS_FIELD)
    if not isinstance(checks, dict):
        problems.add(9, where, f"{item}: a verdict that leaves a {relation} runs the three "
                               "checks (`reading`, `states`, `kind_check`)")
        return
    # every check of the three is reported together: a worker answers one gate run with
    # one rewrite, and the retry cap is what a second refusal costs (issue #175)
    reading = checks.get("reading")
    if not isinstance(reading, list) or not 1 <= len(reading) <= vocab.MAX_CONJUNCTS \
            or not all(_one_line(line) for line in reading):
        problems.add(9, where, f"{item}: `reading` is 1 to {vocab.MAX_CONJUNCTS} one-line "
                               "facts the passage asserts, in your own words")
    else:
        states = checks.get("states")
        places = set(range(1, len(reading) + 1))
        if not isinstance(states, list) or not all(type(s) is int and s in places
                                                   for s in states):
            problems.add(9, where, f"{item}: `states` names the positions of `reading` the "
                                   f"declaration states, each in 1..{len(reading)}")
        elif relation in vocab.STRONG_RELATIONS and set(states) != places:
            problems.add(9, where, f"{item}: a {relation} states every fact of the passage, "
                                   f"and `states` names {sorted(set(states))} of "
                                   f"{sorted(places)}; downgrade to hasPart")
        elif relation == vocab.HAS_PART and not states:
            problems.add(9, where, f"{item}: a hasPart that states no fact of the passage is "
                                   "a relatedMatch")
    if held == vocab.BROAD_MATCH:
        direction = checks.get("direction")
        if not isinstance(direction, dict) or not _nonempty(direction.get("instance")) \
                or not _nonempty(direction.get("hypotheses")):
            problems.add(9, where, f"{item}: a broadMatch gives `direction`: the "
                                   "instantiation that yields the passage, and the "
                                   "hypotheses the passage does not have (a line "
                                   "beginning `none` when there are none)")
        elif not _says_none(direction["hypotheses"]):
            problems.add(9, where, f"{item}: `direction.hypotheses` names a hypothesis the "
                                   "passage does not have, which makes this at most a "
                                   "narrowMatch (R2, PD4); a line that begins with the "
                                   "word `none` says there is none")
    if not _nonempty(checks.get("kind_check")):
        problems.add(9, where, f"{item}: `kind_check` says in one line whether the passage "
                               "is a notation passage or a worked example, and what that "
                               "asks of the declaration")


def rule9_bridge(verdict: dict, left: dict, where: str, item: str, cfg: GateConfig,
                 ctx: Context, problems: Problems) -> None:
    """The lemma a ``relatedMatch`` names, looked up in the declaration index.

    The challenge prompt says a downgrade to ``relatedMatch`` names "the lemma the
    connection needs, which the gate looks up in the index", and until judgment rules 5
    nothing looked anything up: a bridge was free text the tally carried through to the
    audit. A bridge the universe does not hold is no bridge — the connection it claims
    to need is one nobody can check — and the prompt's way out is to write no ``bridge``
    and name the gap in the witness.

    Only on a verdict that leaves a ``relatedMatch``, which is where the tally keeps the
    field; on any other relation it drops it, so a lookup there would refuse a verdict
    over a field nobody reads.
    """
    bridge = verdict.get("bridge")
    if bridge is None or left.get("relation") != vocab.RELATED_MATCH:
        return
    if not isinstance(bridge, dict) \
            or not all(_nonempty(bridge.get(key)) for key in vocab.BRIDGE_KEYS) \
            or not _one_line(bridge.get("why")):
        problems.add(9, where, f"{item}: a bridge is {{{', '.join(vocab.BRIDGE_KEYS)}}}: "
                               "the lemma the connection needs, and one line on what it "
                               "adds")
        return
    rule3_declaration({key: bridge[key] for key in ("library", "declaration")},
                      where, f"{item} (bridge)", cfg, ctx, problems)


def _normal_query(query: str) -> str:
    return " ".join(str(query).split()).casefold()


def _match_tags(tags_of: dict[str, set[str]], needed: list[str]) -> list[str]:
    """The ``needed`` tags left without a query of their own in a maximum matching.

    ``tags_of`` maps each distinct query to the tags it carries; a query serves one tag.
    Augmenting paths over at most a dozen tags.
    """
    owner: dict[str, str] = {}

    def assign(tag: str, seen: set[str]) -> bool:
        for query, tags in tags_of.items():
            if tag in tags and query not in seen:
                seen.add(query)
                if query not in owner or assign(owner[query], seen):
                    owner[query] = tag
                    return True
        return False

    return [tag for tag in needed if not assign(tag, set())]


def rule9_receipt(passage: dict, where: str, item: str, cfg: GateConfig,
                  problems: Problems, ids: list[str], *, passage_kind: Any = None) -> None:
    """An absence's receipt tags every query with the claims it targets, and each claim
    gets a query of **its own**.

    An absence of a clause nobody searched for is no absence, and one query tagged with
    every claim is one query that searched for none of them in particular — so the tags
    must match distinct queries to the claims one for one. A worked example's absence
    also searched for the general theorem it instantiates, tagged ``general``. On a
    located passage the tags are optional, and checked when given.
    """
    if cfg.judgment_rules < 1:
        return
    absent = passage.get("outcome") == vocab.OUTCOME_ABSENT
    receipt = passage.get("receipt") if isinstance(passage.get("receipt"), dict) else {}
    raw = receipt.get("query_clauses")
    if raw is None:
        if absent:
            problems.add(9, where, f"{item}: an absence tags each query with the claims it "
                                   "targets (receipt.query_clauses)")
        return
    if not isinstance(raw, dict):
        problems.add(9, where, f"{item}: receipt.query_clauses is "
                               "{<query>: [<claim id>, ...]}")
        return
    queries = [q for q in receipt.get("queries") or [] if isinstance(q, str)]
    allowed = set(ids) | {vocab.GENERAL_QUERY_TAG}
    tags_of: dict[str, set[str]] = {}
    for query, targets in sorted(raw.items()):
        if query not in queries:
            problems.add(9, where, f"{item}: query_clauses names {query[:40]!r}, which is not "
                                   "one of receipt.queries as written")
            continue
        if not isinstance(targets, list) or not targets \
                or not all(isinstance(t, str) for t in targets) \
                or len(set(targets)) != len(targets):
            problems.add(9, where, f"{item}: query_clauses[{query[:40]!r}] is a non-empty list "
                                   "of distinct claim ids (or general)")
            continue
        unknown = [t for t in targets if t not in allowed]
        if unknown:
            problems.add(9, where, f"{item}: query_clauses[{query[:40]!r}] names "
                                   f"{', '.join(unknown)}, neither a claim id of the passage "
                                   f"({', '.join(ids)}) nor {vocab.GENERAL_QUERY_TAG}")
            continue
        tags_of.setdefault(_normal_query(query), set()).update(targets)
    if not absent:
        return
    untagged = sorted({q for q in queries if q.strip() and q not in raw})
    if untagged:
        problems.add(9, where, f"{item}: an absence tags every query it lists, and "
                               f"{untagged[0][:40]!r}"
                               f"{f' and {len(untagged) - 1} more' if len(untagged) > 1 else ''}"
                               " carries no tag")
    targeted: set[str] = set().union(*tags_of.values()) if tags_of else set()
    unsearched = [c for c in ids if c not in targeted]
    if unsearched:
        problems.add(9, where, f"{item}: no query targets {', '.join(unsearched)}; an absence "
                               "searched for every claim")
    example = (passage_kind or passage.get("passage_kind")) == vocab.PASSAGE_EXAMPLE
    if example and vocab.GENERAL_QUERY_TAG not in targeted:
        problems.add(9, where, f"{item}: a worked example's absence also searched for the "
                               f"general theorem it instantiates, tagged "
                               f"{vocab.GENERAL_QUERY_TAG}")
    if unsearched or (example and vocab.GENERAL_QUERY_TAG not in targeted):
        return
    needed = list(ids) + ([vocab.GENERAL_QUERY_TAG] if example else [])
    left = _match_tags(tags_of, needed)
    if left:
        problems.add(9, where, f"{item}: each claim has a query of its own, and "
                               f"{', '.join(left)} share one with another")


def rule9_verdict(verdict: dict, where: str, cfg: GateConfig, problems: Problems) -> None:
    """What a challenge verdict owes for the action it takes."""
    item = str(verdict.get("item") or "(no item)")
    action = verdict.get("action")
    if not _nonempty(verdict.get("witness")):
        problems.add(9, where, f"{item}: a {action} verdict gives the witness it rests on")
    if action == "downgrade":
        relation = verdict.get("relation")
        if relation not in vocab.LOCATION_RELATIONS:
            problems.add(9, where, f"{item}: a downgrade names the weaker relation it leaves, "
                                   f"not {relation!r}")
    if action == "overturn" and verdict.get("target") in ("absence", "unresolved", "scope"):
        if not isinstance(verdict.get("locations"), list) or not verdict["locations"]:
            problems.add(9, where, f"{item}: an overturn of an {verdict.get('target')} names "
                                   "the locations it found")
    if action == "overturn" and verdict.get("target") == "prominence":
        if not vocab.member(verdict.get("prominence"), vocab.PROMINENCES):
            problems.add(9, where, f"{item}: a prominence overturn names the prominence it "
                                   f"leaves, not {verdict.get('prominence')!r}")
    if action == "uphold" and verdict.get("target") in ("absence", "unresolved"):
        if not isinstance(verdict.get("queries"), list) or not verdict["queries"]:
            problems.add(9, where, f"{item}: an uphold of an {verdict.get('target')} names what "
                                   "the challenger searched before it upheld the record")
    if action == "confirm" and verdict.get("target") == vocab.COVERAGE_TARGET \
            and cfg.judgment_rules >= 3:
        if not isinstance(verdict.get("part_relations"), dict):
            problems.add(9, where, f"{item}: a confirm of a bundle gives each part's relation "
                                   "to the conjuncts it states (part_relations)")
    if action == "refute" and verdict.get("target") == vocab.COVERAGE_TARGET \
            and cfg.judgment_rules >= 3:
        for key in ("unstated", "part_reasons"):
            if verdict.get(key) is None:
                problems.add(9, where, f"{item}: a refute of a bundle names its {key}")


# ---- rule 10: the statement cut -------------------------------------------------------------------


def rule10_sentences(where: str, view: SourceView, rows: list, problems: Problems) -> None:
    """No passage starts inside a sentence but where a rule of the cut starts one.

    The prompt already forbids a cut at a bare "and" or "or", at a list comma, or
    between a hypothesis and its conclusions; until statement-cut rule 4 only the prompt
    did, and the published records carry sentences cut into two to four passages, each
    with a status and links of its own for a claim no fragment states (issue #172).

    **The finding names two passages.** Span-end rules 2 and 3 end a passage at the next
    passage's start, so a head placed inside a sentence truncates the passage before it
    as well: the one that stated the sentence keeps its hypothesis and loses its
    conclusion, and re-cutting either alone leaves the other a fragment. Which end is
    wrong is the worker's call, which is why the refusal names both and not a fix.
    """
    for number, ((start, _end), item) in enumerate(rows):
        if not view.blocks.cuts_a_sentence(start):
            continue
        before = ""
        sentence = view.blocks.sentence_at(start)
        if number and sentence is not None:
            (earlier_start, earlier_end), earlier = rows[number - 1]
            if earlier_end <= start and view.blocks.sentence_at(earlier_start) == sentence:
                before = (f", and truncates {earlier}, the passage of the same sentence "
                          "that ends at that start")
        problems.add(10, where, f"{item}: the passage starts at {start}, inside a sentence and "
                                "at no cut of the rules — a bare \"and\" or \"or\", a list comma "
                                "and the step from a hypothesis to its conclusion are none — so "
                                f"it leaves a fragment that states nothing{before}")


def rule10_cut(doc: dict, where: str, cfg: GateConfig, view: SourceView, placed: dict,
               problems: Problems) -> None:
    """The spans of an inventory are disjoint, bounded and shaped as the cut asks.

    TODO (``TODO_CHECKS``): the sentence, display and connective counts the harvest
    receipt carries, which refuse nothing but are the cut's own measurement.
    """
    if cfg.statement_cut_rule < 1:
        return
    rows = sorted(((span, item) for item, span in placed.items()))
    for (a, b), item in rows:
        for (c, d), other in rows:
            if item >= other:
                continue
            if a < d and c < b:
                problems.add(10, where, f"{item} [{a}, {b}] and {other} [{c}, {d}] share text; "
                                        "the spans of one cut are disjoint, one text one "
                                        "passage")
        # strictly inside one formula, not merely between two adjacent ones: a span that
        # ends where a formula ends is a span that ends after it
        inside = view.blocks.formula_at(b - 1)
        if inside is not None and inside == view.blocks.formula_at(b):
            problems.add(10, where, f"{item}: the span ends inside a formula")
        bound = view.blocks.extent(a, compound=True, continued=True, loose=True,
                                   wide=cfg.statement_cut_rule >= 3)
        if bound is not None and b > bound:
            problems.add(10, where, f"{item}: the span ends at {b}, past the furthest its block "
                                    f"and what its last sentence runs on into reach ({bound})")
    if cfg.statement_cut_rule >= 4:
        rule10_sentences(where, view, rows, problems)
    subjects = []
    for passage in doc.get("passages") or []:
        item = str(passage.get("item"))
        scope = passage.get("scope")
        conjuncts = passage.get("conjuncts")
        if conjuncts is not None:
            shape = passage.get("shape")
            if not isinstance(conjuncts, list) or \
                    not vocab.MIN_CONJUNCTS <= len(conjuncts) <= vocab.MAX_CONJUNCTS:
                problems.add(10, where, f"{item}: conjuncts are {vocab.MIN_CONJUNCTS} to "
                                        f"{vocab.MAX_CONJUNCTS} of them, or none")
            elif scope != vocab.SCOPE_FORMAL_MATH:
                problems.add(10, where, f"{item}: a passage scoped {scope!r} states no "
                                        "compound statement")
            else:
                for number, conjunct in enumerate(conjuncts, 1):
                    if not isinstance(conjunct, dict) or conjunct.get("id") != f"c{number}" \
                            or not _nonempty(conjunct.get("text")):
                        problems.add(10, where, f"{item}: conjunct {number} is "
                                                f"{{id: 'c{number}', text}}")
                if not vocab.member(shape, vocab.COMPOUND_SHAPES):
                    problems.add(10, where, f"{item}: a compound statement names its shape, one "
                                            f"of {', '.join(vocab.COMPOUND_SHAPE_ORDER)}, not "
                                            f"{shape!r}")
                want = vocab.SHAPE_CONJUNCT_COUNTS.get(shape)
                if want is not None and len(conjuncts) != want:
                    problems.add(10, where, f"{item}: a {shape} statement has exactly {want} "
                                            f"conjuncts, not {len(conjuncts)}")
        elif passage.get("shape") is not None:
            problems.add(10, where, f"{item}: a shape belongs to a compound statement, and this "
                                    "passage has no conjuncts")
        if scope != vocab.SCOPE_FORMAL_MATH and \
                passage.get("prominence") != vocab.PROMINENCE_SUPPORTING:
            problems.add(10, where, f"{item}: a passage scoped {scope!r} is supporting, not "
                                    f"{passage.get('prominence')!r}")
        if scope == vocab.SCOPE_FORMAL_MATH and \
                passage.get("prominence") == vocab.PROMINENCE_SUBJECT:
            subjects.append(item)
    if cfg.statement_cut_rule >= 2 and len(subjects) > 1:
        problems.add(10, where, f"{len(subjects)} formal_math passages are marked subject "
                                f"({', '.join(subjects[:4])}); a source has at most one")


# ---- one document ------------------------------------------------------------------------------


def check_document(rel: str, doc: Any, cfg: GateConfig, ctx: Context, problems: Problems, *,
                   task_id: str) -> None:
    """Every rule that applies to one document, in rule order."""
    where = rel
    if not isinstance(doc, dict):
        problems.add(1, where, "a document is a JSON object")
        return
    source_id = rule1_header(doc, where, cfg, problems, task_id)
    if source_id is None:
        return
    view = ctx.view(source_id)
    mode = doc.get("mode")

    frozen = None
    if mode in (vocab.MODE_LOCATE, vocab.MODE_CHALLENGE):
        frozen = _frozen_passages(ctx, task_id, source_id)
    placed = rule2_passages(doc, where, cfg, view, problems, frozen=frozen)

    if mode == vocab.MODE_CHALLENGE:
        records = _frozen_records(ctx, task_id, source_id)
        for number, verdict in enumerate(doc.get("verdicts") or []):
            if not isinstance(verdict, dict):
                problems.add(1, where, f"verdicts[{number}] is not an object")
                continue
            rule9_verdict(verdict, where, cfg, problems)
            item = str(verdict.get("item") or "(no item)")
            record = records.get((item, verdict.get("location_key")))
            if record is not None and cfg.judgment_rules >= 5 \
                    and verdict.get("target") == "location" \
                    and verdict.get("action") in ("confirm", "downgrade"):
                # the relation a verdict leaves is held to every rule a locate note
                # faces, the missing-clause check included (issue #175)
                left = _verdict_location(record, verdict)
                # a downgrade that names no relation of the vocabulary, and a record
                # that carries none, are rule9_verdict's refusal and only its one:
                # these rules are about a relation, and there is none to hold
                if vocab.member(left.get("relation"), vocab.LOCATION_RELATIONS):
                    rule9_location(left, where, item, cfg, problems,
                                   _clause_ids(record), challenger=True)
                    rule9_checks(verdict, left, where, item, problems)
                    rule9_bridge(verdict, left, where, item, cfg, ctx, problems)
            for location in verdict.get("locations") or []:
                if isinstance(location, dict):
                    rule3_declaration(location, where, str(verdict.get("item")), cfg, ctx,
                                      problems)
        return

    rule1_records(doc, where, cfg, ctx, problems)
    rule5_absence(doc, where, cfg, problems)
    if mode == vocab.MODE_INVENTORY:
        rule4_inventory(doc, where, ctx, problems, source_id, cfg)
        rule8_coverage(doc, where, view, placed, problems)
        rule10_cut(doc, where, cfg, view, placed, problems)
        return

    rule4_locate(doc, where, cfg, ctx, problems, source_id, task_id)
    for number, passage in enumerate(doc.get("passages") or []):
        item = str(passage.get("item") or f"passages[{number}]")
        if passage.get("outcome") not in vocab.LOCATE_OUTCOMES:
            problems.add(1, where, f"{item}: outcome {passage.get('outcome')!r} is not one of "
                                   f"{', '.join(sorted(vocab.LOCATE_OUTCOMES))}")
        rule1_coverage(passage, where, item, problems)
        rule6_locations(passage, where, item, cfg, problems)
        base = (frozen or {}).get(item) or {}
        ids = _clause_ids(base)
        for location in passage.get("locations") or []:
            if not isinstance(location, dict):
                problems.add(1, where, f"{item}: a location is an object")
                continue
            rule3_declaration(location, where, item, cfg, ctx, problems)
            if cfg.judgment_rules >= 1:
                rule9_location(location, where, item, cfg, problems, ids)
        if cfg.judgment_rules >= 1:
            rule9_receipt(passage, where, item, cfg, problems, ids,
                          passage_kind=base.get("passage_kind"))


def _frozen_records(ctx: Context, task_id: str, source_id: str) -> dict:
    """``{(item, location_key): record}`` of the challenge task this document answers.

    A challenge task manifest holds ``records`` where a locate one holds ``passages``: a
    record carries the relation the verdict judges, what the worker wrote of it, and —
    on a run that reads bundles — the passage's ``conjuncts``. A manifest with no
    records (an older plan, or a task nobody staged) leaves the verdict rules to
    :func:`rule9_verdict` alone, which is what this gate did before rules 5.
    """
    manifest = ctx.task_manifest(task_id)
    if manifest is None:
        return {}
    out: dict = {}
    for document in manifest.get("documents") or []:
        if str(document.get("source_id")) != source_id:
            continue
        for record in document.get("records") or []:
            if isinstance(record, dict):
                out[(str(record.get("item")), record.get("location_key"))] = record
    return out


def _frozen_passages(ctx: Context, task_id: str, source_id: str) -> dict:
    """``{item: passage}`` of the frozen inventory this task was handed, or ``{}``."""
    manifest = ctx.task_manifest(task_id)
    if manifest is None:
        return {}
    for document in manifest.get("documents") or []:
        if str(document.get("source_id")) != source_id:
            continue
        return {str(p.get("item")): p for p in document.get("passages") or []
                if isinstance(p, dict)}
    return {}


# ---- the command line -------------------------------------------------------------------------------


def _documents(args, cfg: GateConfig, root: pathlib.Path) -> list[tuple[str, Any, str]]:
    """``[(relative path, document, task id)]`` the call is to validate."""
    out: list[tuple[str, Any, str]] = []
    paths: list[pathlib.Path] = []
    if args.findings:
        paths = [pathlib.Path(p) for p in args.findings]
    elif args.changed_files:
        listing = pathlib.Path(args.changed_files)
        try:
            rels = [line.strip() for line in listing.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except OSError as exc:
            raise GateBroken(f"--changed-files {listing} cannot be read ({exc})") from None
        paths = [root / rel for rel in rels]
    else:
        directory = pathlib.Path(args.findings_dir or (root / cfg.findings_dir))
        if not directory.is_dir():
            raise GateBroken(f"no findings directory at {directory}")
        paths = sorted(p for p in directory.glob("*/*.json") if p.is_file())
    for path in paths:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.name
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GateBroken(f"{rel} cannot be read as JSON ({exc})") from None
        out.append((rel, doc, path.parent.name))
    return out


def _log(cfg: GateConfig, line: dict) -> None:
    """One JSON line per call, when the log directory is writable; never fails the gate."""
    if cfg.log_dir is None:
        return
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.log_dir / f"{cfg.run_id}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--gate-config", required=True)
    parser.add_argument("--findings", action="append", default=[],
                        help="a document to validate (repeatable)")
    parser.add_argument("--changed-files", help="a file listing the paths this change touched")
    parser.add_argument("--findings-dir", help="a directory of <task-id>/<source-id>.json")
    parser.add_argument("--root", help="the worktree the paths are relative to (default: the "
                                       "working directory)")
    parser.add_argument("--task-id", help="the task this call is for (else TEND_TASK_ID)")
    parser.add_argument("--pre-merge", action="store_true",
                        help="the batched pre-merge check: several tasks' documents at once")
    parser.add_argument("--self-check", action="store_true",
                        help="validate nothing; say whether the gate can run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    import os  # noqa: PLC0415 — only for the task id the runner exports

    environ = os.environ if env is None else env
    try:
        args = _parse(argv)
    except SystemExit:
        print("FAIL: the gate refuses this command line", file=sys.stderr)
        return EXIT_BROKEN
    # the command line is the label, so a call that could fake it is refused before
    # anything is read, and validates nothing
    if args.findings and args.changed_files:
        print("FAIL: --findings and --changed-files name two different calls", file=sys.stderr)
        return EXIT_BROKEN
    if args.pre_merge and not args.changed_files:
        print("FAIL: --pre-merge is the batched check of --changed-files", file=sys.stderr)
        return EXIT_BROKEN
    if IMPORT_ERRORS:
        print(f"FAIL: the gate directory is incomplete: {'; '.join(IMPORT_ERRORS)}",
              file=sys.stderr)
        return EXIT_BROKEN

    started = time.time()
    try:
        path = pathlib.Path(args.gate_config)
        cfg = GateConfig(read_json(path, "the gate configuration"), path)
        ctx = Context(cfg)
        if args.self_check:
            print(f"OK: the gate can run for {cfg.run_id} [{cfg.mode}]: "
                  f"{len(cfg.sources)} source(s), span-end rule {cfg.span_end_rule}, "
                  f"statement-cut rule {cfg.statement_cut_rule}, judgment rules "
                  f"{cfg.judgment_rules}, declaration index "
                  f"{'yes' if ctx.index is not None else 'no'}")
            for todo in TODO_CHECKS:
                print(f"  TODO, not checked here: {todo}")
            return EXIT_OK
        root = pathlib.Path(args.root or ".").resolve()
        documents = _documents(args, cfg, root)
        problems = Problems()
        task_id = args.task_id or environ.get("TEND_TASK_ID") or ""
        if not task_id and len(documents) == 1:
            # the single-stem rule: one task per change set
            task_id = documents[0][2]
        if args.changed_files:
            rule7_changed_files([rel for rel, _doc, _task in documents], cfg, ctx, problems,
                                task_id=task_id, pre_merge=args.pre_merge)
        for rel, doc, owner in documents:
            check_document(rel, doc, cfg, ctx, problems, task_id=owner or task_id)
    except GateBroken as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_BROKEN
    except Exception as exc:  # noqa: BLE001 — a crash of the gate is never a rejection
        print(f"FAIL: the gate itself failed ({exc.__class__.__name__}: {exc})", file=sys.stderr)
        return EXIT_BROKEN

    caller = ("findings_check" if args.findings else
              "pre_merge" if args.pre_merge else
              "validation" if args.changed_files else "directory")
    by_rule: dict[str, int] = {}
    for rule, _where, _message in problems.rows:
        by_rule[str(rule)] = by_rule.get(str(rule), 0) + 1
    # the keys the spend watchdog reads — ``changed_files`` tells tend's validation call
    # from an agent's own --findings check, and ``pre_merge`` from the batched re-check,
    # so a worker that re-runs the gate neither drains the root nor dilutes its rate
    _log(cfg, {"run_id": cfg.run_id, "caller": caller, "task_id": task_id,
               "mode": cfg.mode, "changed_files": bool(args.changed_files),
               "pre_merge": bool(args.pre_merge), "ok": not problems,
               "exit": EXIT_REJECTED if problems else EXIT_OK,
               "documents": len(documents), "problems": len(problems),
               "by_rule": by_rule, "rules": problems.rules(),
               "reasons": problems.lines()[:20],
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "seconds": round(time.time() - started, 3)})
    if problems:
        print(f"VALIDATION FAILED: {len(problems)} problem(s) in {len(documents)} document(s), "
              f"rule(s) {', '.join(map(str, problems.rules()))}", file=sys.stderr)
        for line in problems.lines():
            print(f"  {line}", file=sys.stderr)
        return EXIT_REJECTED
    print(f"PASS: {len(documents)} document(s) validated for {cfg.run_id} [{cfg.mode}]")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
