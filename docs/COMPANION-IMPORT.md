# Companion annotations in the local Brain store

WikiLean can ingest directories of independent Literature Links annotations into its
existing Brain JSONL/SQLite reader. This is a **local, unreviewed import**, useful for exploring
Autoform blueprints or a book companion and querying every association. The live
wiki remains canonical in D1; imported companions do not modify articles, moderation,
the source registry, the nightly reducer, or a published Brain release.

Literature Links is the interchange format. There is no companion manifest or imposed directory
layout. Each record has one informal source and at most one formal target. A `.json`
file holds one annotation and a `.jsonl` or `.ndjson` file holds one per line; folder
defaults (`_defaults.json`) apply as the package's recursive discovery defines them.
A JSON array is a `SHAPE` problem, not a list of annotations. PDF, LaTeX, HTML, and generic sources all work. No
Autoform-specific extension is required.

## Install the reference validator

The integration uses the Literature Links package **1.0.0** at the commit recorded in
`wiki/src/vendor/literature-links/provenance.json`. The annotation
itself has no version field; the package version identifies the software only. Use
Python 3.12. Install that checkout in your environment:

```sh
python3.12 -m venv /tmp/companion-env
/tmp/companion-env/bin/python -m pip install /absolute/path/to/literature-links
```

The package supplies validation and shard discovery for this Python importer.
WikiLean's native Worker separately vendors the exact pinned schema and generates
a Worker-safe validator from it; the native Python pipeline validates the same
schema. That pin and semantic parity checks are described in [the native runbook](COMPANION-NATIVE.md). The importer checks the package's schema and checker bytes against `provenance.json`, so another build that reports the same version cannot silently pass through. It is optional for ordinary WikiLean work;
ingestion fails with an actionable error if absent. There is no public package URL
or network fallback. Querying an already built index requires
only Python's standard library.

## Import and read

From the WikiLean repository:

```sh
/tmp/companion-env/bin/python -m brain.ingest.companion ingest \
  /path/to/book/annotations /path/to/autoform/annotations.json \
  --source-key my-companion --output /tmp/my-companion-brain

/tmp/companion-env/bin/python -m brain.ingest.companion query \
  --data-dir /tmp/my-companion-brain

/tmp/companion-env/bin/python -m brain.ingest.companion query \
  --data-dir /tmp/my-companion-brain \
  --target 'https://example.org/formal.html#result'
```

The source key is a local label, not a trusted source-registry key. Output must be a
new directory. Every selected input must validate before any output is created. A
malformed selected shard, bad PDF rectangle, invalid JSON, or explicit symlink rejects
the whole batch. Empty imports also fail. The directory is staged and published only
after SQLite construction and its existing integrity/freshness reader succeed.
Existing output is never updated, even if an empty destination appears concurrently; rebuild to a new directory. Atomic no-replace directory publication supports macOS and Linux and fails closed on other platforms.

Use `--include 'annotations/**/*.json'` or `--exclude 'metadata.json'` to select
shards when a root also contains other JSON. Patterns are the reference discovery
API's root-relative shell patterns: `*` can match `/`, and `**/` also matches the root.
Selected unrelated JSON is an error, not a hidden source of dropped records. Put
output outside input roots to avoid selecting generated artifacts on a later scan.

The query emits JSONL with `annotation`, `origin`, `source` and `target` lookup keys,
and per-endpoint `resolution` status. The annotation is the complete original decoded object. `origin` contains
the carrier's absolute base URI and physical JSONL line when relevant; a native
article entry also keeps the JSON pointer of its annotation inside the article.
Discovery supplies file bases; `import_retained(entries, ...)` also accepts native
operational `{annotation, origin}` entries with HTTP or opaque bases, preserving
all origin fields (including an explicit null line).
It is essential context when moving a record that contains relative references:
**references resolve against the containing shard**, not the scan root or source URI.
Query output is a transport report, not a Literature Links shard. To re-export, deliberately
retain the origin or rebase both references; do not discard the origin and relocate
relative records unchanged.

No source or target is fetched, opened, or evaluated. URI resolution only creates
lookup keys. Scheme-qualified references, including opaque URNs and `file:foo`,
remain verbatim. Relative references use the carrier base and literal dot-segment
removal, preserving repeated slashes, Unicode, percent-encoded bytes and their case,
and explicit empty queries/fragments (`?`/`#`). No decoding, host normalization,
resource lookup, or filename guessing occurs.

The core's reference check is lexical, not a complete URI grammar. A malformed
authority such as `https://[oops` or `//[oops` therefore remains valid Literature Links input.
Such records are retained with `resolution.source` or `resolution.target` status
`unresolved` and reason `unsupported_authority_syntax`. An absolute malformed
reference keeps its original lookup key; an unresolved relative reference gets
`urn:wikilean:companion:unresolved:<sha256>` computed from the carrier URI and raw
reference. That key is only an index key, never written into the annotation or
claimed to name a formal object. Use the returned key with `--source`/`--target`
to retrieve those occurrences. A record with no `target` (a checked absence, or
merely an unlinked passage) is indexed under the constant lookup key
`urn:wikilean:companion:absent-target` with `resolution.target` status `absent`;
an endpoint-object target is resolved by its `uri`, and its pin stays inside the
annotation. The other statuses are `absolute` (retained without
resolving) and `resolved` (relative component merging completed); neither certifies
resource existence or full URI validity. Older local imports without this metadata
report `legacy` and can be rebuilt from their original shards. Source metadata, opaque generic anchors, unknown extensions, large
integers, array order, and decoded object insertion order survive a round trip.
An unresolved generic anchor remains a generic anchor, never a whole-resource link.
Duplicate IDs and identical annotations remain separate occurrences. Discovery only
deduplicates overlapping selections of the same carrier file.

## Storage and application boundary

The adapter uses `brain.store.write_sqlite_from_jsonl` and `open_store`, with the
normal `nodes.jsonl`, `edges.jsonl`, `edges_links.jsonl`, and `brain.sqlite3` layout.
Every annotation is one `links` edge with URI endpoints and `unreviewed` confidence.
Its full value, lossless decoded-order JSON string, carrier origin, and occurrence
identity live in the edge evidence. Existing SQLite endpoint indexes support lookups
without rescanning all shards; existing source digest checks reject a stale database.

`nodes.jsonl` is empty on purpose: an arbitrary URI does not establish a Wikidata
concept, registered external database, or verified Lean declaration. This local
projection uses the existing **storage** contract; its raw URI endpoints are not the
production Brain **semantic** node contract. It carries
`profile: companion-local-import-v1` and `publication_status: local-unreviewed`.
It does not create cells or claim compatibility with the cell viewer or release
acceptance gates. The local generation hash binds imported values and transport
origins, so moving shards changes that generation; it is not sealed source authority.

To admit these associations to the published Brain, a reviewed source adapter must
map endpoints to existing semantic identities, establish source/license evidence,
and participate in the normal sealed source plan/replay/release workflow. A schema
validity check is not that review, nor evidence of mathematical equivalence or proof.

Python callers can use:

```python
from brain.ingest.companion import import_companions, iter_annotations

report = import_companions(["/path/to/shards"], source_key="example", output="/tmp/index")
for row in iter_annotations("/tmp/index", target="urn:lean:My.result"):
    print(row["annotation"], row["origin"])
```

To build several local views from the same input generation, discover once and pass
that captured collection to each consumer. `import_collected(collection,
source_key=..., output=...)` revalidates and copies all annotation values in memory,
but never reopens shards or resolves their captured absolute carrier paths again.
This preserves a coherent capture even if files change or directories are moved
between discovery and publication. Invalid mutated records still reject the batch.

```python
from literature_links.discovery import collect
from brain.ingest.companion import import_collected

captured = collect(["/path/to/shards"])
report = import_collected(captured, source_key="example", output="/tmp/captured-index")
# The same captured collection can also feed Autoform's companion catalog.
```

## Validation

`brain/test_companion_ingest.py` tests the adapter against the real Brain builder,
SQLite indexes, provenance recovery, duplicates, freshness, atomic failure cleanup,
existing-output protection, and URI-resolution edge cases. These standard-library tests run in the normal
Python CI gate. Optional reference tests additionally exercise all four Literature Links modes,
semantic/JSON failures, arbitrary shards, include/exclude, relative origins, and CLI
round trips. Run the complete boundary suite in the Literature Links environment:

```sh
/tmp/companion-env/bin/python brain/test_companion_ingest.py --require-reference
```

The flag fails if Literature Links is absent; the standard CI gate reports those tests as skipped
until the package is available to the public CI environment.

For the cross-project Autoform → Literature Links → WikiLean flow and Worker export, see
[the integration guide](COMPANION-INTEGRATION.md).
