# One local workspace for all three projects

WikiLean stores complete FCA records natively. Autoform's Markdown articles remain
independent informal sources; separate records associate them with formal results.
This workspace captures selected records once and supplies the same values to
WikiLean's local Brain SQLite index and Autoform's native attachment reader.
It does not call the legacy WikiLean or workflow-derived Autoform exporters.

The FCA schema stays unversioned: one source, one target, and optional location,
id, extensions and schema hint. Workspace configuration and record-origin envelopes
are application data, not companion schemas. The development package is 2.0.0a2.

## Create a workspace

Use the local integration checkouts, `uv`, Git, and a directory outside all three
tool repositories. An Autoform consumer is an independent project, not its product
checkout. Either the project root or its `blueprint/` directory is accepted.

```sh
python3.12 scripts/companion-workspace.py --workspace /absolute/workspace init \
  --autoform-root /absolute/autoform \
  --fca-root /absolute/formal-companion-annotations \
  --consumer /absolute/lean-project \
  --shards /absolute/annotations \
  --article /absolute/native-article.json
```

The consumer, shards, and article inputs are optional. The workspace creates an
`inbox/` for individual annotations, arrays, JSONL or NDJSON. Repeated `--shards`,
`--include`, and `--exclude` select any directory arrangement supported by FCA's
reader. Keep reports and generated workspace history outside the shard selection.
No repository URL, commit, or Node exporter is needed for native synchronization.
Older configuration coordinates are retained as unused operational metadata.

An article input is a saved native WikiLean snapshot with `schema_version: 4` and
an `annotations` array of review items containing `links` and `unlinked`. The 4 is
WikiLean's **application storage marker**, never a field on an FCA record. Rejected
items and targetless occurrences remain in WikiLean; the shared association index
contains only existing public links. Relative references require the original
`base_uri` on their link envelope. No reference is reconstructed from old fields.
Legacy snapshots fail with an explicit migration-required error.

Setup installs the local editable Autoform and FCA packages into one Python 3.12
environment using Autoform's lock. If interrupted, finish with `setup` in place of
`init`. Repeat setup after package definitions or the dependency lock change.
It never modifies the live D1 database or runs formalization/proof workers.

## Refresh and view

```sh
/absolute/workspace/companion doctor
/absolute/workspace/companion sync
/absolute/workspace/companion serve --port 8743
/absolute/workspace/companion status
/absolute/workspace/companion query --target 'urn:lean:Example.result'
```

[The local viewer](http://127.0.0.1:8743/) searches original source/formal references
and shows stored records and selectors. It listens only on loopback, reads without
writing, and follows the selected generation on each request.

Each generation contains `index/brain.sqlite3` and
`autoform-native/records.jsonl`. The latter is a lossless operational archive:
each entry has `annotation` and its original `origin`, including `base_uri` and
pointer. It must not be mistaken for a naked FCA shard. Relative references never
start resolving against the generated archive's location.

With a consumer configured, sync registers one stable archive reference in its
`.autoform-companions.json`. Existing selections and bindings are preserved. Later
syncs switch `workspace/current`, so both readers follow the same capture without
creating numbered Markdown copies. Use `--no-attach-sources` to omit registration.

To attach records to an existing article, add an explicit binding in the consumer:

```json
{
  "archives": ["/absolute/workspace/current/autoform-native/records.jsonl"],
  "bindings": [{
    "source": "https://example.org/pinned/wiki/article.md",
    "article": "blueprint/roadmap/result.md",
    "sha256": "SHA256_OF_THE_EXACT_AUTHORED_MARKDOWN_BYTES"
  }]
}
```

Bindings are Autoform application configuration. An article ID can replace the
path. The recorded digest checks that the local article matches the deliberately
bound revision; it does not fetch or verify the remote resource. Changed articles
show historical associations. Unbound sources appear together in a generated
companion index. Unknown selectors remain visible and unresolved. Neither the
article text nor its proof graph, readiness, or verification state is changed.
Autoform's local renderer generates the view, including its Obsidian projection.

## Refresh guarantees and failure handling

A refresh validates selected shards with the FCA reference reader, reads native
article links, and detaches one in-memory capture. Both consumers receive that
capture; a final comparison checks full values and origins, including duplicates.
The completed generation records tool fingerprints and package versions before
one atomic `current` symlink replacement.

Invalid or empty inputs, mismatched outputs, or tool changes before selection
leave the old generation active. Generation directories remain for diagnosis.
Initial Autoform configuration registration happens after a valid selection; if
registration fails, sync reports the error and preserves the valid selected index
and its interruption record. Fix the configuration and retry. Once registered,
refreshing normally requires only the single shared-pointer update.

The workspace lock serializes setup and synchronization. An interrupted lock is
never stolen: inspect its PID before removing it. Do not feed retained archives
or historical generated catalogs back into annotation discovery. No sync pushes,
publishes, deploys, or writes back into the canonical WikiLean application database.

## Verification

```sh
/absolute/workspace/.venv/bin/python brain/test_companion_workspace.py --require-integrations
/absolute/workspace/.venv/bin/python brain/test_companion_ingest.py --require-reference
/absolute/workspace/.venv/bin/python scripts/check-companion-integration.py \
  --autoform /absolute/workspace/.venv/bin/autoform \
  --node /absolute/node22 --output /absolute/new-rehearsal-directory
```

The rehearsal copies a synthetic SQLite database, migrates the copy, verifies that
HTTP returns its stored annotations unchanged, exercises all four source modes
and duplicate occurrences, and renders native attachments on the original
Autoform article without changing its bytes. The original database and reports
are retained alongside the result for inspection.
