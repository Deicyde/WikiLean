#!/usr/bin/env python3
"""Stage 9a: fetch and render a sourceset's sources — the one command that may fetch.

docs/PIPELINE.md section 3 (stage 9a). It walks the sources a sourceset's newest
version names, fetches the ones whose bytes it does not already hold, renders every one
of them under :data:`render_web.RENDERER`, and writes the next manifest version with
each entry's three shas, its renderer and its ``fetch`` block filled in.

**The network is behind an explicit ``--execute``, and nothing else.** With no flag the
command is a dry run: it prints what it would ask for and makes no request. With
``--fixtures <dir>`` it replays recorded responses — which is what the tests use.
``--execute`` and ``--fixtures`` together are refused, so a run cannot half-replay.
This is the shape ``brain/acquire-*.sh`` takes in this repository.

A revision is fetched **once**: a source whose ``source.raw.html`` is already on disk is
re-read from it and re-checked, never asked for again, because a re-fetch of an old
revision can answer with other bytes (templates expand as they are now) and a recorded
layer never changes.

    python3 scripts/pipeline/fetch_sources.py --dir <sourceset> --source <id> … \\
        [--fixtures <dir> | --execute] [--render-only]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import render_web  # noqa: E402
import sourceset as sourceset_mod  # noqa: E402
import sources as sources_mod  # noqa: E402
import wikinet  # noqa: E402

#: the site each family is fetched from. One family, one site: a source id names it.
SITES = {"enwiki": "https://en.wikipedia.org"}


class FetchProblem(ValueError):
    """A source this command will not fetch, render or record."""


def site_of(family_id: str) -> str:
    try:
        return SITES[family_id]
    except KeyError:
        raise FetchProblem(f"no site for family {family_id!r} (known: "
                           f"{', '.join(sorted(SITES))})") from None


def raw_bytes(directory: Path, revision: int, transport, site: str) -> tuple[bytes, bool]:
    """``(the revision's raw response, whether it was fetched now)``.

    A ``source.raw.html`` already on disk is re-checked and re-used: a revision is
    fetched once.
    """
    path = directory / "source.raw.html"
    if path.is_file():
        raw = path.read_bytes()
        wikinet.check_parse(raw, revision)
        return raw, False
    if transport is None:
        raise FetchProblem(f"{path} is missing and this is a dry run: pass --execute to "
                           f"fetch it, or --fixtures <dir> to replay a recording")
    return wikinet.fetch_parse(transport, site, revision), True


def prepare(ss: sourceset_mod.Sourceset, entry: dict, transport, *,
            render_only: bool = False) -> dict:
    """Fetch (unless already held) and render one source; return its filled-in entry."""
    source_id = entry["source_id"]
    directory = ss.source_dir(source_id)
    directory.mkdir(parents=True, exist_ok=True)
    if render_only and not (directory / "source.raw.html").is_file():
        raise FetchProblem(f"{source_id}: --render-only, and no source.raw.html is held")
    raw, fetched = raw_bytes(directory, entry["revision"], None if render_only else transport,
                             site_of(entry["family"]))
    if fetched:
        render_web.write_bytes(directory / "source.raw.html", raw)
    rendering = render_web.render(render_web.canonical_body(raw))
    render_web.write_rendering(directory, rendering)
    filled = dict(entry)
    filled["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    filled["html_sha256"] = rendering.html_sha256
    filled["layer_sha256"] = rendering.layer_sha256
    filled["renderer"] = render_web.RENDERER
    filled["fetch"] = wikinet.fetch_block()
    return filled


def run(directory: Path, source_ids: list[str], *, execute: bool, fixtures: Path | None,
        render_only: bool, sourceset_id: str | None = None) -> int:
    if execute and fixtures is not None:
        raise FetchProblem("--execute and --fixtures are not used together: one is the "
                           "network and the other is a recording")
    ss = sourceset_mod.Sourceset.open(directory, sourceset_id)
    manifest = ss.latest()
    wanted = source_ids or sorted(manifest.preferred().values())
    missing = [sid for sid in wanted if manifest.get(sid) is None]
    if missing:
        raise FetchProblem(f"the newest version holds no entry for "
                           f"{', '.join(missing[:5])}")
    todo = [sid for sid in wanted
            if not (ss.source_dir(sid) / "source.raw.html").is_file()]
    transport = None
    if fixtures is not None:
        transport = wikinet.FixtureTransport(fixtures)
    elif execute:
        transport = wikinet.CurlTransport()
    if transport is None:
        site = site_of(manifest.family)
        print(f"dry run: {len(wanted)} source(s), {len(todo)} to fetch from {site}; "
              f"pass --execute to fetch, or --fixtures <dir> to replay")
        for sid in todo[:20]:
            entry = manifest.get(sid)
            print(f"  GET {wikinet.request_url(site, wikinet.parse_params(entry['revision']))}")
        if len(todo) > 20:
            print(f"  … and {len(todo) - 20} more")
        return 0
    entries = dict(manifest.entries)
    changed = 0
    for sid in wanted:
        entries[sid] = prepare(ss, manifest.get(sid), transport, render_only=render_only)
        changed += entries[sid] != manifest.get(sid)
    if not changed:
        print(f"{ss.id}: version {manifest.version} already records every one of "
              f"{len(wanted)} source(s)")
        return 0
    new = ss.write_version(entries)
    ss.refresh_state(command="fetch", section={"sources": len(wanted),
                                               "fetched": len(todo), "changed": changed})
    print(f"{ss.id}: version {new.version} ({'delta' if new.is_delta else 'snapshot'}), "
          f"{changed} entry(ies) changed, manifest {new.sha256}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path, help="the sourceset directory")
    parser.add_argument("--id", help="its id (the directory's name by default)")
    parser.add_argument("--source", action="append", default=[], dest="source_ids",
                        help="a source id; repeatable (every planned source by default)")
    parser.add_argument("--execute", action="store_true",
                        help="actually fetch from the site (the only flag that does)")
    parser.add_argument("--fixtures", type=Path, help="replay recorded responses instead")
    parser.add_argument("--render-only", action="store_true",
                        help="render what is already held and fetch nothing")
    args = parser.parse_args(argv)
    try:
        return run(args.dir, args.source_ids, execute=args.execute, fixtures=args.fixtures,
                   render_only=args.render_only, sourceset_id=args.id)
    except (FetchProblem, wikinet.WikiError, sourceset_mod.SourcesetError,
            sources_mod.SourceProblem, render_web.RenderError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
