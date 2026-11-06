#!/usr/bin/env python3
"""Stage 9a: the web-source families a record set may quote.

docs/PIPELINE.md section 3 (stage 9a). This is the families: which site a source belongs
to, what its revisions are permalinked as, and whether its licence lets a record quote
it. The rest of the stage is beside it — :mod:`sourceset` (the append-only manifest),
:mod:`render_web` (the ``web-html/3`` renderer, written as a use of
``site/anchor_resolve.text_layer``), :mod:`wikinet` and :mod:`fetch_sources` (the one
client, behind an explicit ``--execute``).

Metadata about a public site only: a title, a container title, an SPDX licence id and a
permalink template. No page, no revision, no text (AGENTS.md: WikiLean is public).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import vocab  # noqa: E402


class SourceProblem(ValueError):
    """A source whose family, licence or identity a record set may not carry."""


@dataclass(frozen=True)
class Family:
    """One web-source family: what every revision of every page of one site shares."""

    id: str
    title: str
    container_title: str
    licence_class: str
    spdx: str
    revision_url: str

    def revision_link(self, pageid: int, revision: int) -> str:
        """The permalink of one revision."""
        if isinstance(pageid, bool) or not isinstance(pageid, int) or pageid < 1:
            raise SourceProblem(f"a page id is a positive integer, not {pageid!r}")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceProblem(f"a revision is a positive integer, not {revision!r}")
        return self.revision_url.format(pageid=pageid, revision=revision)

    def source_id(self, pageid: int, revision: int) -> str:
        self.revision_link(pageid, revision)  # refuses a bad page id or revision
        return f"{self.id}-{pageid}-{revision}"


#: The families this pipeline knows. English Wikipedia is the first locate corpus;
#: a page title is metadata only, since titles hold slashes and move, so a source is
#: keyed by page id and revision.
FAMILIES = {
    "enwiki": Family(
        id="enwiki",
        title="English Wikipedia",
        container_title="Wikipedia",
        licence_class="open_share_alike",
        spdx="CC-BY-SA-4.0",
        revision_url="https://en.wikipedia.org/w/index.php?curid={pageid}&oldid={revision}",
    ),
}


def family(family_id: str) -> Family:
    """The family ``family_id``, or a refusal naming it."""
    found = FAMILIES.get(str(family_id))
    if found is None:
        raise SourceProblem(f"no web-source family {family_id!r} (known: "
                            f"{', '.join(sorted(FAMILIES))})")
    return found


def family_of(source_id: str) -> Family:
    """The family a ``<family>-<pageid>-<revision>`` source id names."""
    if not isinstance(source_id, str) or source_id.count("-") < 2:
        raise SourceProblem(f"{source_id!r} is not a <family>-<pageid>-<revision> source id")
    return family(source_id.rsplit("-", 2)[0])


def licence_problem(fam: Family) -> str | None:
    """Why a family's licence keeps its sources out of a record set, or None.

    Every exported record quotes its passage, and there is no redacted quote to fall
    back on, so a source whose licence class does not admit quoting keeps its whole set
    out. A class that asks for attribution needs the licence named, since the record
    carries it.
    """
    if fam.licence_class not in vocab.OPEN_LICENCE_CLASSES:
        return (f"licence class {fam.licence_class!r}: an exported record quotes the source, and "
                f"only {', '.join(sorted(vocab.OPEN_LICENCE_CLASSES))} may be quoted")
    if fam.licence_class in vocab.ATTRIBUTION_LICENCE_CLASSES and not fam.spdx:
        return (f"licence class {fam.licence_class!r} needs the licence named, and family "
                f"{fam.id} has no SPDX id")
    return None


def source_record(entry: dict) -> dict:
    """One ``sources.jsonl`` row with the three fields its records need beside it.

    ``entry`` is the sourceset entry: ``source_id``, ``pageid``, ``revision``, ``title``,
    ``wikidata``, ``html_sha256``, ``layer_sha256``, ``renderer`` and ``assessment``. The
    permalink, the container title and the SPDX licence come from the family, never from
    the entry, so a set cannot name a licence its family does not have.
    """
    source_id = str(entry.get("source_id") or "")
    fam = family_of(source_id)
    pageid, revision = entry.get("pageid"), entry.get("revision")
    if fam.source_id(pageid, revision) != source_id:
        raise SourceProblem(f"source_id {source_id!r} does not agree with pageid {pageid!r} and "
                            f"revision {revision!r}")
    why = licence_problem(fam)
    if why:
        raise SourceProblem(f"{source_id}: {why}")
    out = {key: entry.get(key) for key in vocab.SOURCE_KEYS}
    out["uri"] = fam.revision_link(pageid, revision)
    out["container_title"] = fam.container_title
    out["licence"] = fam.spdx
    return out
