#!/usr/bin/env python3
"""Stage 9: the prompts a model stage runs on, rendered from the run's own paths.

docs/PIPELINE.md section 3 (stages 9b, 9c and 9d). The templates are WikiLean's own,
under ``scripts/pipeline/prompts/``; they were written against formal-companion's for
the rules both sides' gates enforce, because a prompt that asked for anything else would
be refused by :mod:`gate.verify_locate` after the model time was paid for.

Four files are written into a planned root's ``prompts/`` directory, one per role and
turn:

* ``worker-system.md`` — the universe, the tools, the data-hygiene rules, and how to
  read the gate's exit status. One file for all three modes;
* ``worker.md`` — the mode's own task prompt (``inventory-worker``, ``locate-worker``,
  ``challenge-worker``);
* ``reviewer-system.md`` and ``reviewer.md`` — form and the gate's output. **Judgment
  is not the reviewer's**: it lives in the challenge (9d), which is a run of its own with
  its own records and its own gate. A reviewer that argued about mathematics would be a
  second, unrecorded judge of the same passage.

**No conditional blocks.** formal-companion's templates carry `{{#if}}` arms for the
rule versions a plan may run under; WikiLean's prompts are written for the one set its
gate enforces — span-end rule 3, statement-cut rule 3, judgment rules 4 — and a change
of rule is a change of prompt, in the same commit as the gate. :func:`render` refuses a
template with an unfilled slot, so a prompt can never reach a worker with a literal
``{{tasks_dir}}`` in it.

**Every path a prompt names is one inside the worker's own working directory**, which
the runner stages before the call (``run/workdir.py``), and :func:`slots` refuses an
absolute one. A sandboxed worker reads nothing outside that directory and writes
nothing outside one directory under it, so a prompt that named the run root or the
sourceset would be asking for what cannot happen — which is exactly what it did until
2026-11-06. For the same reason **no prompt tells anyone to run a command**: the gate
and the passage ids are the runner's steps, between the worker and the reviewer.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE, HERE / "gate"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import locate_text  # noqa: E402 — scripts/pipeline/gate/locate_text.py
import vocab  # noqa: E402

TEMPLATE_DIR = HERE / "prompts"
#: the four files a planned root's ``prompts/`` holds, in the order they are written
PROMPT_FILES = ("worker-system.md", "worker.md", "reviewer-system.md", "reviewer.md")
#: which template each mode's ``worker.md`` is rendered from
WORKER_TEMPLATES = {
    vocab.MODE_INVENTORY: "inventory-worker.md.tmpl",
    vocab.MODE_LOCATE: "locate-worker.md.tmpl",
    vocab.MODE_CHALLENGE: "challenge-worker.md.tmpl",
}
SHARED_TEMPLATES = {"worker-system.md": "worker-system.md.tmpl",
                    "reviewer-system.md": "reviewer-system.md.tmpl",
                    "reviewer.md": "reviewer.md.tmpl"}
#: The judgment rules — R1 to R7, the policy defaults and the readings — are one text,
#: rendered into the ``{{judgment_rules}}`` slot of the locate and challenge prompts.
#: formal-companion keeps two copies of them, one per prompt, and they have drifted;
#: here a change is one edit and both prompts move together.
RULES_TEMPLATE = "judgment-rules.md.part"

#: the gate's exit vocabulary, read from the gate itself so a prompt cannot drift from
#: it; a test pins these to ``verify_locate``'s own constants and printed lines
EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_BROKEN = 3
REJECTED_MARK = "VALIDATION FAILED"
BROKEN_MARK = "FAIL:"

_SLOT = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


class PromptProblem(ValueError):
    """A template that cannot be rendered, or a slot nobody filled."""


def render(template: str, values: dict) -> str:
    """``template`` with every ``{{slot}}`` filled; an unfilled one is a refusal."""
    missing: list[str] = []

    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            missing.append(name)
            return match.group(0)
        return str(values[name])

    out = _SLOT.sub(fill, template)
    if missing:
        raise PromptProblem(f"the template leaves {', '.join(sorted(set(missing)))} "
                            f"unfilled")
    return out


def read_template(name: str) -> str:
    path = TEMPLATE_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptProblem(f"no prompt template at {path}") from None


def gate_exits(role: str) -> str:
    """How a ``role`` reads the gate's verdict; one paragraph in every prompt.

    **Nobody in a prompt runs the gate.** The runner does, after the worker finishes
    and before the reviewer is called, because a sandboxed agent has no shell — the
    exits below are what the runner saw, handed on.
    """
    broken = (f"**exit {EXIT_BROKEN}** (a `{BROKEN_MARK} …` line), any other non-zero "
              f"exit, or an exit {EXIT_REJECTED} with no `{REJECTED_MARK}` list (a crash) "
              f"means the gate itself could not run")
    if role == "reviewer":
        return (f"**exit {EXIT_REJECTED}**, which always comes with a `{REJECTED_MARK}` "
                f"list, is a verdict on the document: request changes naming what it "
                f"refused. {broken}: an infrastructure fault, not grounds for rejecting "
                f"the worker; say so in your notes.")
    return (f"**exit {EXIT_OK}** passes, and the run goes on. On **exit "
            f"{EXIT_REJECTED}**, which always comes with a `{REJECTED_MARK}` list, you "
            f"are called again with that list and the document you wrote: fix exactly "
            f"what it names and write the document again. {broken}: an infrastructure "
            f"fault, not a verdict on your work — say so in your `notes` and do not "
            f"weaken your document to make it pass.")


def slots(*, run_id: str, mode: str, sources_dir: str, findings_dir: str,
          candidates_dir: str = "", index_dir: str = "", gate_verdict_file: str = "",
          universe: str = "", universe_sha256: str = "") -> dict:
    """Every slot the templates name, from the run's configuration and the vocabulary.

    The directory slots are relative to the **worker's own working directory**, which
    the runner stages (``run/workdir.py``): a prompt names no absolute path and no path
    outside that directory, because a sandboxed worker can reach nothing else. They are
    taken as given here, so a contribute package can name its own.
    """
    if mode not in vocab.MODES:
        raise PromptProblem(f"{mode!r} is no locate mode (one of {sorted(vocab.MODES)})")
    for name, value in (("sources_dir", sources_dir), ("findings_dir", findings_dir),
                        ("candidates_dir", candidates_dir), ("index_dir", index_dir),
                        ("gate_verdict_file", gate_verdict_file)):
        if value and (Path(value).is_absolute() or ".." in Path(value).parts):
            raise PromptProblem(
                f"{name} is {value!r}: a prompt names a path inside the worker's own "
                "working directory, and a worker can reach nothing else")
    return {
        "run_id": run_id,
        "mode": mode,
        "schema": vocab.DOCUMENT_SCHEMA,
        "sources_dir": sources_dir,
        "findings_dir": findings_dir,
        "candidates_dir": candidates_dir or "(this run hands out no candidates)",
        "index_dir": index_dir or "(this run stages no declaration search)",
        "gate_verdict_file": gate_verdict_file or "(no gate verdict was staged)",
        "universe": universe,
        "universe_sha256": universe_sha256,
        "max_locations": vocab.MAX_LOCATIONS,
        "min_absence_queries": vocab.MIN_ABSENCE_QUERIES,
        "min_conjuncts": vocab.MIN_CONJUNCTS,
        "max_conjuncts": vocab.MAX_CONJUNCTS,
        "max_conjunct_chars": vocab.MAX_CONJUNCT_CHARS,
        "quote_max_words": vocab.QUOTE_MAX_WORDS,
        "compound_shapes": _list(vocab.COMPOUND_SHAPE_ORDER),
        "relations": _list(vocab.LOCATION_RELATIONS),
        "strong_relations": _list(r for r in vocab.LOCATION_RELATIONS
                                  if r in vocab.STRONG_RELATIONS),
        "conventions": _list(vocab.CONVENTIONS),
        "verdict_actions": _list(sorted(vocab.VERDICT_ACTIONS)),
        "challenge_targets": _list(sorted(vocab.CHALLENGE_TARGETS)),
        "min_coverage_parts": vocab.MIN_COVERAGE_PARTS,
        "simple_clause": vocab.SIMPLE_STATEMENT_CLAUSE,
        "cut_flags": _list(sorted(vocab.CUT_FLAGS)),
        "proof_statuses": _list(sorted(vocab.PROOF_STATUSES)),
        "prominences": _list(["subject", "central", "supporting"]),
        "scopes": _list([vocab.SCOPE_FORMAL_MATH, vocab.SCOPE_EXPOSITORY,
                         vocab.SCOPE_NON_MATH]),
        "passage_kinds": _list(sorted(vocab.KINDS)),
        "cut_connectives": _list(locate_text.CUT_CONNECTIVES),
        "worker_exits": gate_exits("worker"),
        "reviewer_exits": gate_exits("reviewer"),
    }


def _list(values) -> str:
    return ", ".join(f"`{value}`" for value in values)


def render_all(mode: str, values: dict) -> dict[str, str]:
    """``{file name: text}`` for every file of :data:`PROMPT_FILES`."""
    if mode not in WORKER_TEMPLATES:
        raise PromptProblem(f"{mode!r} is no locate mode")
    values = {**values,
              "judgment_rules": render(read_template(RULES_TEMPLATE), values).rstrip("\n")}
    out = {"worker.md": render(read_template(WORKER_TEMPLATES[mode]), values)}
    for name, template in SHARED_TEMPLATES.items():
        out[name] = render(read_template(template), values)
    return {name: out[name] for name in PROMPT_FILES}


def write(directory: Path, mode: str, values: dict) -> dict[str, str]:
    """Write the four prompts into ``directory``; returns ``{file name: text}``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rendered = render_all(mode, values)
    for name, text in rendered.items():
        (directory / name).write_text(text, encoding="utf-8")
    return rendered
