# Browse companion links locally

The companion viewer opens an imported Literature Links index in a read-only browser interface.
It shows informal sources, formal targets, source locations, carrier origins, and
complete original annotations. Search narrows links by source or target text;
optional exact filters use the endpoint lookup keys returned by the API.

After [importing a companion](COMPANION-IMPORT.md), run from WikiLean:

```sh
python3.12 -m brain.companion_server --data-dir /tmp/my-companion-brain --port 8743
```

Open [the local companion viewer](http://127.0.0.1:8743/). The server uses Python's
standard library and the existing Brain SQLite reader; the optional Literature Links package is
not needed for browsing an already validated import. Stop it with Ctrl-C.

For the integrated workspace, use the coordinator in
[COMPANION-WORKSPACE.md](COMPANION-WORKSPACE.md). Its viewer points at
`WORKSPACE/current/index`. Each request resolves `current` once, so it stays on one
complete generation even if the coordinator advances that symlink during the request.
The viewer never changes the selected generation itself. The coordinator also passes
`--generation-root WORKSPACE/generations`; the viewer checks that each request's
selected index stays under that directory, including after a later pointer change.

## Interface

- Search finds a literal substring in source and target lookup keys. ASCII letters
  are matched without case; Unicode and percent-encoded characters remain literal.
- “Match a specific link” filters by exact source or formal lookup key. Normal
  records use resolved URI keys; opaque absolute references stay unchanged.
- Each card shows the original source/target references, its location, carrier file
  and entry, full annotation, and reference-resolution details. Unknown extensions,
  duplicate IDs, arrays, and large integers remain visible without a JavaScript
  numeric conversion.
- Only HTTP(S) references become clickable links. File paths, custom schemes, and
  unresolved references display as text. Opening a web link requires a deliberate
  click; the viewer does not fetch source or target content.
- Pagination shows 25 records by default. If a workspace rebuild changes the
  selected generation, an old pagination link asks you to refresh the results.

The viewer does not render a source PDF/HTML document or prove that a locator still
matches it. It displays the annotation's own source and location information.
It does not publish, moderate, edit D1, launch proofs, or export relocated records.
The original shards remain the export format; a JSON response includes origins and
is a transport report rather than a new Literature Links shard format.

## Local read API

`GET /api/report` returns the imported association count, local source key,
generation, profile, and publication status.

`GET /api/annotations` returns:

```json
{
  "records": [
    {
      "ordinal": 0,
      "annotation": {"source": {"type": "generic", "uri": "../notes.md"}, "target": "urn:lean:Example.result"},
      "origin": {"base_uri": "file:///example/links/notes.jsonl", "line": 1},
      "source": "file:///example/notes.md",
      "target": "urn:lean:Example.result",
      "resolution": {"source": {"status": "resolved"}, "target": {"status": "absolute"}}
    }
  ],
  "offset": 0,
  "limit": 25,
  "total": 1,
  "next_offset": null,
  "generation": "sha256:…"
}
```

Query parameters: `q` (literal source/target substring), `source`, `target` (exact
lookup keys), `offset`, `limit`, and optional `generation`. Each is singular;
unknown/repeated parameters fail with 400. Results are bounded to 1–100 records,
with offsets 0–1,000,000. Text search is limited to 2,048 characters; other values
to 4,096. Exact endpoint filters use the existing SQLite indexes. The generation
parameter returns 409 on change rather than mixing pages from different imports;
this applies to both API endpoints and the browser page.
A missing, stale, or wrong-profile index returns 503; the server never rebuilds or
falls back to a different data source. The usual Brain JSONL freshness checks run
when opening each request's index.

## Access boundary and validation

The server binds only `127.0.0.1`. Requests must name its actual loopback host and
port (`127.0.0.1:PORT` or `localhost:PORT`); an Origin header, if present, must match
that exact HTTP origin. Cross-site requests, cross-origin reads, and write methods
are rejected. Responses disable caching and embedding, use a restrictive content
policy, and provide no cross-origin access. Annotation text is HTML-escaped;
query values are bound SQL parameters. These controls prevent web pages from
using a different host or origin to read local annotation data. Other processes on
the same computer are not authenticated by this local viewer.

Run `python3.12 brain/test_companion_server.py`. The hermetic tests use real local
HTTP requests and a generated SQLite fixture to cover browsing, exact filters,
pagination, literal searches, full-value preservation, hostile HTML/URI input,
Host/Origin guards, read-only behavior, stale data, and generation changes.
They run in `scripts/ci-python.sh` without a Literature Links installation.
