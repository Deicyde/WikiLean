# One local workspace for all three projects

The workspace connects Autoform's authored blueprint, the FCA single-annotation
reference package, and WikiLean's SQLite store and local browser. It installs one
Python 3.12 environment from the local checkouts and Autoform's dependency lock.
Annotations require only `source` and `target`, with no schema version field.
Workspace configuration, receipts and
generation reports are operational files, not another companion format.

## Create a workspace

Use the integration checkouts of Autoform and WikiLean and the unversioned FCA draft
from the reference package's 2.0.0a2 development build.
You need `uv`, Git, and a directory outside the tool checkouts. From WikiLean:

```sh
python3.12 scripts/companion-workspace.py --workspace /absolute/path/to/workspace init \
  --autoform-root /absolute/path/to/autoform \
  --fca-root /absolute/path/to/formal-companion-annotations \
  --consumer /absolute/path/to/lean-project \
  --repo https://github.com/owner/lean-project --commit FULL_COMMIT_SHA \
  --shards /absolute/path/to/another-companion
```

Consumer coordinates are optional as a group: omit all three to use only imported
annotations. A consumer is an independent Autoform project, not the Autoform product
checkout. Both its project root and its `blueprint/` directory are accepted. The
commit is explicit; refreshing never silently follows a new HEAD. Edit the
`consumer` coordinates in `workspace.json` when deliberately changing projects or
revisions. This command does not initialize a Lean project, plan work, run proofs,
push Git refs, deploy a Worker, or publish a package.

`--shards` can be repeated; `--include` and `--exclude` select arbitrary layouts
using the reference reader's patterns. The workspace also creates an `inbox/`:
drop individual annotations, JSON arrays or JSONL/NDJSON shards there. Keep other
reports outside selected roots. Workspace history cannot itself be an input root.
The producer outputs are always selected independently of user shard filters.
With source attachment enabled, do not scan the consumer's entire `blueprint/sources`
directory or an ancestor: later scans would encounter generated catalog reports.
Select a dedicated input subdirectory such as `blueprint/sources/external`, or use
`--no-attach-sources`. The workspace checks this before generating a catalog.

To include an explicit local WikiLean article snapshot, add:

```sh
  --node /absolute/path/to/node22 --article /absolute/path/to/article.json
```

The file must be one native WikiLean article object with `slug`, `version`, `revid`,
`annotations`, and `wikipedia_title` (or `wikipediaTitle`). This accepts a saved API
read or a deliberate local fixture; it neither reads nor writes live D1. Setup
installs `wiki/` dependencies with the supplied Node 22. Already-exported FCA shards
need no Node runtime. Unpinned articles fail export. Native entries without formal
targets remain producer diagnostics instead of fabricated links.

If installation stops, finish it with:

```sh
python3.12 scripts/companion-workspace.py --workspace /absolute/path/to/workspace setup
```

Setup never replaces an existing workspace. Its launcher uses the shared virtual
environment and the configured WikiLean checkout. Autoform and FCA are editable
local installs. A receipt binds their dependency inputs; after changing a package
definition, Autoform's lock or a used Node lock, run `companion setup` again.

## Refresh, browse, and use evidence in Autoform

The installed workspace has a `companion` launcher:

```sh
/absolute/path/to/workspace/companion doctor
/absolute/path/to/workspace/companion sync
/absolute/path/to/workspace/companion serve --port 8743
```

Open [the local browser](http://127.0.0.1:8743/). It searches source and formal links
and shows their locations and original annotations. The server is read-only and
listens only on this computer. Stop it with Ctrl-C. It follows the current snapshot
on each request; there is no need to restart it after a refresh.

When a consumer is configured, each successful refresh adds a new Markdown source
catalog under its `blueprint/sources/companion-<generation>/`. The report gives its
path and the corresponding blueprint-relative citation. Autoform's existing local
renderer and Obsidian can read these pages. Each imported occurrence has its own
page, including duplicates. The untouched annotation and original carrier base are
retained in an operational `records.jsonl` archive beside the pages. This archive
is not a relocated FCA shard and should not be fed back into shard discovery.

Existing source indexes, roadmap articles, dependency edges and proof state are
not rewritten. Import makes evidence available for ordinary source review; it does
not turn an external status into an Autoform assertion. `--no-attach-sources` at
initialization keeps the same catalog inside the workspace instead of adding it to
the consumer. Older source catalogs remain available after later refreshes.

```sh
/absolute/path/to/workspace/companion status
/absolute/path/to/workspace/companion query --target 'urn:lean:Example.result'
```

Query output includes the complete original annotation, its origin and URI lookup
keys. Only annotation records themselves use the FCA schema. These responses are
transport reports. The current index lives at `WORKSPACE/current/index`.

## Refresh guarantees

A refresh performs this sequence:

1. Check the shared environment, local package origins and dependency receipt.
2. Export the committed Autoform associations and selected local WikiLean articles.
3. Capture selected shards once with the reference validator and discovery API.
4. Feed that same capture to WikiLean's SQLite builder and Autoform's source catalog.
5. Compare complete values and occurrence origins in both outputs, including duplicates.
6. Record tool checkout fingerprints and installed package versions, then atomically
   switch the single `current` symlink to the completed generation.

Each generation is built at its final path from the beginning. It is never renamed
after capture, so generated carrier URIs remain valid. External shards retain their
original bases; a relative reference never starts resolving against the workspace.
Malformed or empty combined input leaves the previous active snapshot unchanged.
An individual producer may emit no links while other valid inputs remain, with its
omissions recorded. Unknown locations and extensions stay intact.

The workspace lock serializes setup and refresh. It is never stolen automatically.
After an interrupted process, inspect `.workspace.lock` and confirm its recorded PID
is no longer using the workspace before removing that lock manually. An interruption
after selection preserves the already-active catalog. Failed generations remain
unselected for diagnosis, and existing user files are not overwritten.

`generations/<id>/generation.json` records the tool commits, dirty-code fingerprints,
installed distributions, producer diagnostics, selected consumer and output paths.
New generations have new carrier identities even when their mathematical endpoints
are unchanged. Occurrence identity is not a claim of mathematical identity.

The workspace is deliberately local. It does not make imported URI endpoints
eligible for WikiLean's published semantic Brain graph or write article annotations
back into D1. Those remain separate application adapters; all three tools already
exchange and consume the atomic records here without expanding the core schema.

## Check the lifecycle

```sh
/absolute/path/to/workspace/.venv/bin/python brain/test_companion_workspace.py --require-integrations
/absolute/path/to/workspace/.venv/bin/python brain/test_companion_ingest.py --require-reference
/absolute/path/to/workspace/.venv/bin/python brain/test_companion_server.py
```

The workspace tests cover shared capture despite changing input files, exact
recovery, failed and empty refreshes, mismatched consumers, interruption after
selection, independent project paths, locks, dependency receipts and tool identity.
The ordinary Python CI gate runs the standard-library cases; the shared environment
is required for the actual cross-project cases.
