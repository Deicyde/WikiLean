#!/usr/bin/env python3
"""Stage 2: the revision-free keys of a record set, and the one JSON spelling.

docs/PIPELINE.md section 3 (stage 2). A ``cf-locate-recordset/2`` record carries three
keys that name a judgment without naming the revision it was made on or the commit it
was made against, so a verdict on one survives a re-cut, a re-render and a new universe:

* ``passage_key`` = sha256("<pageid>\\n<span_sha256>") — this passage of this page;
* ``pair_key``    = sha256("<passage_key>\\n<repository>\\n<declaration>") — this
  passage paired with this declaration, in whatever library holds it;
* ``bundle_key``  = sha256 of the passage key and the parts' pair keys, sorted, one per
  line — this coverage bundle.

They are load-bearing beyond the format: ``atlas_feedback`` rows carry ``pair_key``, so
a change to any of these three byte strings re-keys every reader's vote. The producer
writes them here; ``scripts/articles/build_store.py`` recomputes them and refuses a
record whose keys disagree, and ``scripts/pipeline/test_keys.py`` pins the two
implementations to each other.

:func:`canonical` is the one JSON spelling every sealed file uses: sorted keys, no
spaces, UTF-8, no ASCII escaping. A sealed set is bytes, and this is the function that
makes two exports of one run byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


class KeyProblem(ValueError):
    """A value a key cannot be made from, named rather than hashed anyway."""


def canonical(value: Any) -> str:
    """The one JSON spelling of a record, a seal or a receipt."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_line(value: Any) -> str:
    """:func:`canonical` with the newline a JSONL line ends in."""
    return canonical(value) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _one_line(value: Any, name: str) -> str:
    """A non-empty one-line string with no edge whitespace: a key's parts are joined by \\n."""
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value:
        raise KeyProblem(f"a key's {name} is a non-empty one-line string with no edge whitespace, "
                         f"not {value!r}")
    return value


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HEX64.match(value):
        raise KeyProblem(f"a key's {name} is a 64-hex sha256, not {value!r}")
    return value


def passage_key(pageid: Any, span_sha256: str) -> str:
    """``sha256("<pageid>\\n<span_sha256>")``: this passage of this page, revision-free.

    ``span_sha256`` is the sha256 of the passage's span on the text layer, so the key
    moves with the passage's *text* and not with the revision it was cut from.
    """
    if isinstance(pageid, bool) or not isinstance(pageid, int) or pageid < 1:
        raise KeyProblem(f"a pageid is a positive integer, not {pageid!r}")
    return sha256_text(f"{pageid}\n{_sha(span_sha256, 'span_sha256')}")


def pair_key(passage: str, repository: str, declaration: str) -> str:
    """``sha256("<passage_key>\\n<repository>\\n<declaration>")``: the judged pair, commit-free.

    ``repository`` is the lean end's ``uri``, not the universe's library id: a library
    may be renamed or split and the pair is the same pair.
    """
    return sha256_text("\n".join([_sha(passage, "passage_key"),
                                  _one_line(repository, "repository"),
                                  _one_line(declaration, "declaration")]))


def sha256_json(value: Any) -> str:
    """sha256 of :func:`canonical`'s bytes: the same object always hashes the same."""
    return sha256_text(canonical(value))


def span_sha256(layer: str, span: Iterable[int]) -> str:
    """The sha256 of a passage's text: the layer's code points ``[start, end)``, UTF-8.

    The value every record's subject end carries, so a consumer holding the records
    alone can compute :func:`passage_key`.
    """
    pair = list(span)
    if (len(pair) != 2 or not all(type(offset) is int for offset in pair)
            or not 0 <= pair[0] < pair[1] <= len(layer)):
        raise KeyProblem(f"a span is [start, end] with 0 <= start < end <= {len(layer)}, "
                         f"not {span!r}")
    return sha256_text(layer[pair[0]:pair[1]])


def cut_id(pageid: Any, layer_sha256: str, spans: Iterable[Iterable[int]]) -> str:
    """The identity of one cut of one page: its pageid, its layer and its spans.

    ``sha256`` of the canonical ``{layer_sha256, pageid, spans}``, the spans sorted. Two
    inventories that divide the same layer the same way have one cut id, whatever their
    runs, their scopes or the items they gave the passages.
    """
    out = []
    for span in spans:
        pair = list(span)
        if len(pair) != 2 or not all(type(offset) is int for offset in pair) \
                or not 0 <= pair[0] < pair[1]:
            raise KeyProblem(f"a span is [start, end] with 0 <= start < end, not {span!r}")
        out.append([pair[0], pair[1]])
    if isinstance(pageid, bool) or not isinstance(pageid, int) or pageid < 1:
        raise KeyProblem(f"a pageid is a positive integer, not {pageid!r}")
    return sha256_json({"pageid": pageid, "layer_sha256": _sha(layer_sha256, "layer_sha256"),
                        "spans": sorted(out)})


def location_key(source_id: str, item: str, library: str, commit: str,
                 declaration: str) -> str:
    """``sha256("<source_id>\\n<clean item>\\n<library>\\n<commit>\\n<declaration>")``.

    The run-free identity of one location: the same passage located at the same
    declaration of the same commit by two runs is one key, where :func:`finding_id` —
    which names the finding inside one run — is two. It is the key a consumer upserts a
    record on and the key a challenge plan and its verdicts agree on, so every part is
    required: a key with an empty library, commit or declaration would collide across
    everything that lost the same field.

    Unlike the three ``/2`` keys above this one names a commit, which is why those
    exist: a verdict keyed here does not survive a new universe.
    """
    import items  # local: items imports nothing from here

    parts = [str(source_id or "").strip(), items.clean(str(item or "")),
             str(library or "").strip(), str(commit or "").strip(),
             str(declaration or "").strip()]
    names = ("source_id", "item", "library", "commit", "declaration")
    missing = [name for name, part in zip(names, parts) if not part]
    if missing:
        raise KeyProblem(f"a location_key needs every part; {', '.join(missing)} is empty")
    return sha256_text("\n".join(parts))


def finding_id(run_id: str, source_id: str, item: str, kind: str,
               unfaithful_decls: Iterable[str]) -> str:
    """``sha256("\\n".join([run_id, source_id, clean(item), kind, *sorted(decls)]))``.

    The id a finding row carries, content-addressed within one run: ``run_id`` is the
    first part, so the same passage answered by two runs gets two ids. ``source_id`` is
    a part because an item is the pair ``(source_id, item)``, and the declarations are
    joined in as separate parts rather than pre-joined, so a declaration carrying the
    separator cannot forge another row's id.
    """
    import items  # local: items imports nothing from here

    parts = [str(run_id), str(source_id), items.clean(str(item)), str(kind),
             *sorted(str(decl) for decl in unfaithful_decls)]
    return sha256_text("\n".join(parts))


def bundle_key(passage: str, pair_keys: Iterable[str]) -> str:
    """The passage key and the parts' pair keys, sorted, one per line.

    Sorted, so the bundle's key does not depend on the order the producer listed its
    parts in; at least two distinct parts, as a bundle stands on a compound statement.
    """
    keys = [_sha(key, "pair_key") for key in pair_keys]
    if len(keys) < 2 or len(set(keys)) != len(keys):
        raise KeyProblem(f"a bundle has at least two distinct parts, not {len(set(keys))}")
    return sha256_text("\n".join([_sha(passage, "passage_key"), *sorted(keys)]))
