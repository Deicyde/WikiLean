#!/usr/bin/env python3
"""Fixture unit tests for build_common's v2 external layer (SCHEMA.md v2).

Exercises, against a tiny synthetic catalog/data/external/ in a tempdir:
minting policy (anchored + 1-hop frontier, per-db cap, frontier ordered by
inbound links), the snippet license guard, links-edge context dedup, concept
projection dedup, page-qid xref minting, the `f` facet bit table, unit
assembly, the literature paper layer (paper minting + paper→statement
contains + OpenAlex bibliography links + absence degrade), and the two-file
edge writer (write_edges: non-links rows +
full meta → edges.jsonl, links rows + own meta → edges_links.jsonl). No
network, no real catalog inputs — build() itself is covered by the ordinary
rebuild + test_acceptance.py.

Run: python3 brain/test_v2.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_common as bc  # noqa: E402
from build_context import (  # noqa: E402
    external_pair_generation,
    seal_external_pair_meta,
)
from ingest import common as ingest_common  # noqa: E402

REG = {
    "nlab": {"url_template": "https://ncatlab.org/nlab/show/{id}",
             "ingest": {"snippets": True}},
    "mathworld": {"url_template": "https://mathworld.wolfram.com/{id}.html",
                  "ingest": {"snippets": False}},
    "stacks": {"url_template": "https://stacks.math.columbia.edu/tag/{id}",
               "ingest": {"snippets": True}},
}

FAILURES: list[str] = []


def check(name: str, cond: bool, note: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {note}" if note and not cond else ""))
    if not cond:
        FAILURES.append(name)


def write_jsonl(path: Path, db: str, rows: list[dict]) -> None:
    with path.open("w") as f:
        f.write(json.dumps({"_meta": {"db": db, "n": len(rows)}}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")


def page(db: str, pid: str, **kw) -> dict:
    return {"db": db, "id": pid, "title": kw.pop("title", pid),
            "url": f"https://example.org/{db}/{pid}", **kw}


def make_external(d: Path) -> None:
    write_jsonl(d / "nlab_pages.jsonl", "nlab", [
        # a: anchored via existing xref edge; carries a licensed snippet
        page("nlab", "a", snippet="idea of a", snippet_license="nLab (attribution)"),
        # b: anchored via CC0 qid -> graph concept Q2
        page("nlab", "b", qid="Q2", kind_hint="definition"),
        page("nlab", "c"),   # frontier: 1 hop from b
        page("nlab", "d"),   # 2 hops (via c) — never minted
        page("nlab", "e"),   # frontier with 2 inbound links (cap priority)
        page("nlab", "f"),   # frontier with 1 inbound link
    ])
    write_jsonl(d / "nlab_links.jsonl", "nlab", [
        {"db": "nlab", "src": "a", "dst": "b", "context": "body"},
        {"db": "nlab", "src": "a", "dst": "b", "context": "statement"},  # dup, better ctx
        {"db": "nlab", "src": "b", "dst": "a", "context": "related"},
        {"db": "nlab", "src": "b", "dst": "c", "context": "body"},
        {"db": "nlab", "src": "c", "dst": "d", "context": "body"},
        {"db": "nlab", "src": "a", "dst": "e", "context": "body"},
        {"db": "nlab", "src": "b", "dst": "e", "context": "body"},
        {"db": "nlab", "src": "b", "dst": "f", "context": "body"},
    ])
    # snippet on a no-content source: the build must strip it (license guard)
    write_jsonl(d / "mathworld_pages.jsonl", "mathworld", [
        page("mathworld", "M1", qid="Q1", snippet="MUST NOT be stored"),
    ])
    # a db with no source_registry key: the whole file is skipped
    write_jsonl(d / "bogusdb_pages.jsonl", "bogusdb", [page("bogusdb", "x")])


def run_layer(d: Path, cap: int = 8000, xref_pairs: set | None = None):
    ext_data = bc.load_external(d, REG)
    return bc.external_layer(
        ext_data,
        concept_qids={"Q1", "Q2"},
        xref_dsts={"xref:nlab:a"},
        concept_anchor={"xref:nlab:a": {"Q1"}},
        xref_pairs=xref_pairs if xref_pairs is not None else {("Q1", "xref:nlab:a")},
        registry=REG, cap=cap)


def test_env_override() -> None:
    old = os.environ.get("BRAIN_EXTERNAL_DIR")
    try:
        os.environ["BRAIN_EXTERNAL_DIR"] = "/nonexistent/ext"
        check("env: BRAIN_EXTERNAL_DIR override",
              bc.external_dir() == Path("/nonexistent/ext"))
        check("env: missing dir loads as no-op",
              bc.load_external(bc.external_dir(), REG) == {})
    finally:
        if old is None:
            os.environ.pop("BRAIN_EXTERNAL_DIR", None)
        else:
            os.environ["BRAIN_EXTERNAL_DIR"] = old


def test_loading(d: Path) -> None:
    ext_data = bc.load_external(d, REG)
    check("load: registered dbs only (bogusdb skipped)",
          sorted(ext_data) == ["mathworld", "nlab"], f"got {sorted(ext_data)}")
    check("load: _meta rows skipped", len(ext_data["nlab"]["pages"]) == 6)


def test_external_pair_publication(d: Path) -> None:
    pair_dir = d / "generated-pairs"
    pair_dir.mkdir()
    registry = {"fixture": {"ingest": {"snippets": False}},
                "emptydb": {"ingest": {"snippets": False}},
                "first": {"ingest": {"snippets": False}},
                "legacy": {"ingest": {"snippets": False}},
                "unicode": {"ingest": {"snippets": False}}}
    pages = [page("fixture", "p1"), page("fixture", "p2")]
    links = [{"db": "fixture", "src": "p1", "dst": "p2",
              "context": "body"}]
    with mock.patch.object(ingest_common, "EXTERNAL_DIR", pair_dir):
        with mock.patch.object(
            ingest_common, "now_iso", return_value="2020-01-01T00:00:00+00:00"
        ):
            ingest_common.emit("fixture", pages, links, {"source_pin": "old"})

        pages_path = pair_dir / "fixture_pages.jsonl"
        links_path = pair_dir / "fixture_links.jsonl"
        first_pair_bytes = (pages_path.read_bytes(), links_path.read_bytes())
        with mock.patch.object(
            ingest_common, "now_iso", return_value="2030-01-01T00:00:00+00:00"
        ):
            ingest_common.emit(
                "fixture",
                [dict(row) for row in pages],
                [dict(row) for row in links],
                {"source_pin": "old"},
            )
        check(
            "pair: identical normalized rows emit byte-identically across clocks",
            (pages_path.read_bytes(), links_path.read_bytes()) == first_pair_bytes,
        )
        pages_meta = json.loads(pages_path.read_text().splitlines()[0])["_meta"]
        links_meta = json.loads(links_path.read_text().splitlines()[0])["_meta"]
        generation = pages_meta.get("pair_generation", "")
        check("pair: writer seals identical generation metadata",
              pages_meta == links_meta
              and pages_meta.get("pair_schema")
              == ingest_common.EXTERNAL_PAIR_SCHEMA
              and generation.startswith("sha256:") and len(generation) == 71
              and "fetched_at" not in pages_meta)
        page_rows = [json.loads(line) for line in pages_path.read_text().splitlines()[1:]]
        link_rows = [json.loads(line) for line in links_path.read_text().splitlines()[1:]]
        changed_audit = dict(pages_meta, fetched_at="2099-01-01T00:00:00+00:00",
                             n_fetches_this_run=999)
        check("pair: audit timestamps and run counters do not change identity",
              external_pair_generation(changed_audit, page_rows, link_rows)
              == generation)

        prior_pair_bytes = (pages_path.read_bytes(), links_path.read_bytes())
        rejected_meta = 0
        for field, value in (
            ("fetched_at", "2030-01-01T00:00:00+00:00"),
            ("n_fetches_this_run", 1),
            ("n_api_calls", 2),
            ("fetch_budget_left", 3),
            ("max_fetch", 4),
            ("unknown_metadata", "ambient"),
        ):
            try:
                ingest_common.emit(
                    "fixture",
                    [dict(row) for row in pages],
                    [dict(row) for row in links],
                    {"source_pin": "old", field: value},
                )
            except ValueError:
                rejected_meta += 1
        for extra in (
            {},
            {"source_pin": ""},
            {"source_pin": "old", "n_with_qid": True},
            {"source_pin": "old", "sitemap_inventory": -1},
        ):
            try:
                ingest_common.emit(
                    "fixture",
                    [dict(row) for row in pages],
                    [dict(row) for row in links],
                    extra,
                )
            except ValueError:
                rejected_meta += 1
        check(
            "pair: writer rejects audit, run-local, invalid, and unknown metadata",
            rejected_meta == 10
            and (pages_path.read_bytes(), links_path.read_bytes())
            == prior_pair_bytes,
        )
        check("pair: writer emits canonical JSON object ordering",
              pages_path.read_text().splitlines()[1]
              == json.dumps(page_rows[0], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False))
        check("pair: sealed output passes the build reader",
              len(bc.load_external(pair_dir, registry)["fixture"]["pages"]) == 2)

        legacy_path = pair_dir / "legacy_pages.jsonl"
        legacy_path.write_text(
            json.dumps({
                "_meta": {
                    "db": "legacy",
                    "fetched_at": "2020-01-01T00:00:00+00:00",
                    "n_fetches_this_run": 7,
                }
            })
            + "\n"
            + json.dumps(page("legacy", "old"))
            + "\n"
        )
        check(
            "pair: reader retains unsealed legacy audit-metadata compatibility",
            bc.load_external(pair_dir, registry)["legacy"]["pages"][0]["id"]
            == "old",
        )

        # A page-only source still publishes its links half as a meta-only file.
        ingest_common.emit("emptydb", [page("emptydb", "only")], [],
                           {"source_pin": "empty"})
        empty_links = pair_dir / "emptydb_links.jsonl"
        check("pair: zero-link generation has an explicit partner",
              empty_links.exists()
              and len(empty_links.read_text().splitlines()) == 1
              and bc.load_external(pair_dir, registry)["emptydb"]["links"] == [])

        decomposed = "Igor Kr\u030ciz\u030c"
        ingest_common.emit(
            "unicode",
            [page("unicode", decomposed, title=decomposed)],
            [],
            {"source_pin": "unicode"},
        )
        unicode_page = bc.load_external(pair_dir, registry)["unicode"]["pages"][0]
        check("pair: upstream non-NFC text is preserved and sealed",
              unicode_page["id"] == decomposed and unicode_page["title"] == decomposed)

        journal_dir = d / "journal-only"
        journal_dir.mkdir()
        journal_child = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'brain' / 'ingest'))
import common
common.EXTERNAL_DIR = Path(sys.argv[2])
real_replace = os.replace
def replace(src, dst):
    real_replace(src, dst)
    if Path(dst).name.endswith('.transaction.json'):
        os.kill(os.getpid(), signal.SIGKILL)
common.os.replace = replace
common.emit('first', [{'db':'first','id':'p','title':'P',
                       'url':'https://example.test/p'}], [],
            {'source_pin':'first'})
"""
        journal_killed = subprocess.run(
            [sys.executable, "-c", journal_child,
             str(Path(__file__).resolve().parents[1]), str(journal_dir)],
            capture_output=True,
            text=True,
        )
        journal_rejected = validation_rejected = False
        try:
            bc.load_external(journal_dir, registry)
        except ValueError:
            journal_rejected = True
        try:
            ingest_common.validate_external_directory(journal_dir)
        except ValueError:
            validation_rejected = True
        check("pair: journal-only first publication fails closed",
              journal_killed.returncode < 0 and journal_rejected
              and validation_rejected)
        with mock.patch.object(ingest_common, "EXTERNAL_DIR", journal_dir):
            ingest_common.emit(
                "first", [page("first", "p")], [], {"source_pin": "recovered"}
            )
        check("pair: next publisher recovers journal-only crash",
              len(bc.load_external(journal_dir, registry)["first"]["pages"]) == 1
              and not list(journal_dir.glob(".wikilean-pair-*"))
              and not list(journal_dir.glob(".*.tmp")))

        old_pages = pages_path.read_bytes()
        old_links = links_path.read_bytes()
        replace_targets: list[str] = []
        real_replace = os.replace
        page_commit_failed = False

        def fail_page_commit(src, dst):
            nonlocal page_commit_failed
            target = Path(dst).name
            if target in {"fixture_pages.jsonl", "fixture_links.jsonl"}:
                replace_targets.append(target)
            if (target == "fixture_pages.jsonl"
                    and not page_commit_failed):
                page_commit_failed = True
                raise OSError("simulated crash before pages commit")
            return real_replace(src, dst)

        newer_pages = pages + [page("fixture", "p3")]
        newer_links = [{"db": "fixture", "src": "p2", "dst": "p3",
                        "context": "proof"}]
        crashed = False
        try:
            with mock.patch.object(ingest_common.os, "replace",
                                   side_effect=fail_page_commit):
                ingest_common.emit("fixture", newer_pages, newer_links,
                                   {"source_pin": "new"})
        except OSError:
            crashed = True
        check("pair: links publish before the pages commit point",
              crashed and replace_targets[:2]
              == ["fixture_links.jsonl", "fixture_pages.jsonl"])
        check("pair: caught interruption restores the prior generation",
              pages_path.read_bytes() == old_pages
              and links_path.read_bytes() == old_links
              and len(bc.load_external(pair_dir, registry)["fixture"]["pages"]) == 2)
        check("pair: caught interruption cleans journal and staging files",
              not list(pair_dir.glob(".wikilean-pair-*"))
              and not list(pair_dir.glob(".*.tmp")))

        # A real SIGKILL leaves the durable journal in place. The reader uses
        # the hard-linked prior generation instead of exposing the mixed pair.
        signal_path = d / "sigkill-ready"
        child = """
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'brain' / 'ingest'))
import common
common.EXTERNAL_DIR = Path(sys.argv[2])
real_replace = os.replace
def replace(src, dst):
    real_replace(src, dst)
    if Path(dst).name == 'fixture_links.jsonl':
        Path(sys.argv[3]).write_text('ready')
        os.kill(os.getpid(), signal.SIGKILL)
common.os.replace = replace
common.emit('fixture', json.loads(sys.argv[4]), json.loads(sys.argv[5]),
            {'source_pin': 'sigkill'})
"""
        killed = subprocess.run(
            [sys.executable, "-c", child,
             str(Path(__file__).resolve().parents[1]), str(pair_dir),
             str(signal_path), json.dumps(newer_pages), json.dumps(newer_links)],
            capture_output=True,
            text=True,
        )
        fallback = bc.load_external(pair_dir, registry)["fixture"]
        check("pair: SIGKILL reader falls back to prior generation",
              killed.returncode < 0 and signal_path.exists()
              and len(fallback["pages"]) == 2
              and fallback["links"][0]["dst"] == "p2")

        # Re-running first recovers the stale journal, then publishes normally.
        ingest_common.emit("fixture", newer_pages, newer_links,
                           {"source_pin": "new"})
        loaded = bc.load_external(pair_dir, registry)["fixture"]
        check("pair: rerun heals the pair", len(loaded["pages"]) == 3
              and loaded["links"][0]["dst"] == "p3"
              and not list(pair_dir.glob(".wikilean-pair-*"))
              and not list(pair_dir.glob(".*.tmp")))

        # Two publishers are serialized. A pauses after its links rename while
        # B blocks on the lock; B must become the final complete generation.
        concurrency_child = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'brain' / 'ingest'))
import common
common.EXTERNAL_DIR = Path(sys.argv[2])
pin, signal_path = sys.argv[3], sys.argv[4]
if signal_path:
    real_replace = os.replace
    def replace(src, dst):
        real_replace(src, dst)
        if Path(dst).name == 'fixture_links.jsonl':
            Path(signal_path).write_text('locked')
            time.sleep(0.5)
    common.os.replace = replace
pages = [{'db':'fixture','id':f'{pin}1','title':f'{pin} one',
          'url':f'https://example.test/{pin}/1'},
         {'db':'fixture','id':f'{pin}2','title':f'{pin} two',
          'url':f'https://example.test/{pin}/2'},
         {'db':'fixture','id':f'{pin}3','title':f'{pin} three',
          'url':f'https://example.test/{pin}/3'}]
links = [{'db':'fixture','src':f'{pin}1','dst':f'{pin}2','context':'body'}]
common.emit('fixture', pages, links, {'source_pin': pin})
"""
        lock_signal = d / "publisher-a-locked"
        base_args = [sys.executable, "-c", concurrency_child,
                     str(Path(__file__).resolve().parents[1]), str(pair_dir)]
        publisher_a = subprocess.Popen(
            base_args + ["A", str(lock_signal)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 5
        while not lock_signal.exists() and publisher_a.poll() is None \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        publisher_b = subprocess.Popen(
            base_args + ["B", ""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _a_out, _a_err = publisher_a.communicate(timeout=10)
        _b_out, _b_err = publisher_b.communicate(timeout=10)
        concurrent = bc.load_external(pair_dir, registry)["fixture"]
        final_meta = json.loads(pages_path.read_text().splitlines()[0])["_meta"]
        check("pair: concurrent publishers serialize complete generations",
              lock_signal.exists() and publisher_a.returncode == 0
              and publisher_b.returncode == 0
              and final_meta["source_pin"] == "B"
              and concurrent["pages"][0]["id"] == "B1")

        before_invalid = (pages_path.read_bytes(), links_path.read_bytes())
        invalid_rejected = 0
        for bad_pages in (
            [],
            [dict(page("fixture", "bad"), _meta={"spoof": True})],
            [dict(page("fixture", "bad"), score=1.5)],
            [dict(page("fixture", "bad"), title=2)],
        ):
            try:
                ingest_common.emit("fixture", bad_pages, [],
                                   {"source_pin": "invalid"})
            except (RuntimeError, ValueError):
                invalid_rejected += 1
        try:
            ingest_common.emit(
                "fixture",
                [page("fixture", "bad-link")],
                [{"db": "fixture", "src": "bad-link", "dst": "other",
                  "context": 3}],
                {"source_pin": "invalid"},
            )
        except (RuntimeError, ValueError):
            invalid_rejected += 1
        check("pair: empty, reserved, and noncanonical rows fail before publish",
              invalid_rejected == 5
              and (pages_path.read_bytes(), links_path.read_bytes())
              == before_invalid)

        verifier_rejected = 0
        for case, bad_pages, count_override in (
            ("empty", [], None),
            ("malformed", [{"db": "fixture", "id": "missing-fields"}], None),
            ("boolean-count", [page("fixture", "one")], True),
        ):
            invalid_dir = d / f"sealed-{case}"
            invalid_dir.mkdir()
            base_meta = {"db": "fixture", "fetched_at": "audit",
                         "n_pages": len(bad_pages), "n_links": 0}
            if count_override is not None:
                base_meta["n_pages"] = count_override
            sealed_meta = seal_external_pair_meta(base_meta, bad_pages, [])
            for suffix in ("pages", "links"):
                rows = bad_pages if suffix == "pages" else []
                with (invalid_dir / f"fixture_{suffix}.jsonl").open("w") as fh:
                    fh.write(json.dumps({"_meta": sealed_meta}) + "\n")
                    for row in rows:
                        fh.write(json.dumps(row) + "\n")
            try:
                bc.load_external(invalid_dir, registry)
            except ValueError:
                verifier_rejected += 1
        check("pair: verifier rejects empty, malformed, and boolean-count seals",
              verifier_rejected == 3)

        orphan_dir = d / "orphan-pair"
        orphan_dir.mkdir()
        orphan = orphan_dir / "fixture_links.jsonl"
        orphan.write_bytes(links_path.read_bytes())
        direct_rejected = explicit_rejected = validation_rejected = False
        unregistered_rejected = False
        try:
            bc.load_external(orphan_dir, registry)
        except ValueError:
            direct_rejected = True
        try:
            fake = SimpleNamespace(path=orphan,
                                   logical_path="fixture_links.jsonl", pin="fixture")
            bc.load_external(None, registry, page_files=(), link_files=(fake,))
        except ValueError:
            explicit_rejected = True
        try:
            fake_page = SimpleNamespace(
                path=pages_path, logical_path="fixture_pages.jsonl", pin="fixture"
            )
            fake_link = SimpleNamespace(
                path=links_path, logical_path="fixture_links.jsonl", pin="fixture"
            )
            bc.load_external(
                None, {}, page_files=(fake_page,), link_files=(fake_link,)
            )
        except ValueError:
            unregistered_rejected = True
        try:
            ingest_common.validate_external_directory(orphan_dir)
        except ValueError:
            validation_rejected = True
        check("pair: readers reject orphan and unregistered bound inputs",
              direct_rejected and explicit_rejected and validation_rejected
              and unregistered_rejected)

        # A matching label is not enough: the generation is recomputed over
        # both row sets, so same-count content tampering also fails closed.
        lines = links_path.read_text().splitlines()
        tampered = json.loads(lines[1])
        tampered["context"] = "statement"
        links_path.write_text(lines[0] + "\n" + json.dumps(tampered) + "\n")
        rejected = False
        try:
            bc.load_external(pair_dir, registry)
        except ValueError:
            rejected = True
        check("pair: reader rejects content tampering", rejected)


def test_minting(d: Path) -> None:
    ext_nodes, edges, stats = run_layer(d)
    ids = {n["id"] for n in ext_nodes}
    check("mint: anchored via xref dst", "xref:nlab:a" in ids)
    check("mint: anchored via page qid", "xref:nlab:b" in ids)
    check("mint: 1-hop frontier minted", {"xref:nlab:c", "xref:nlab:e",
                                          "xref:nlab:f"} <= ids)
    check("mint: 2-hop page NOT minted", "xref:nlab:d" not in ids)
    check("mint: stats count", stats["minted"] == {"mathworld": 1, "nlab": 5},
          f"got {stats['minted']}")
    b = next(n for n in ext_nodes if n["id"] == "xref:nlab:b")
    check("mint: payload shape", b["type"] == "ext" and b["db"] == "nlab"
          and b["label"] == "b" and b["url"].endswith("/nlab/b")
          and b["qid"] == "Q2" and b["kind_hint"] == "definition")


def test_content_pin_ignores_path_and_mtime(d: Path) -> None:
    first = d / "first.xml.gz"
    second_dir = d / "relocated"
    second_dir.mkdir()
    second = second_dir / "second.xml.gz"
    payload = b"same exact compressed source bytes\n"
    first.write_bytes(payload)
    second.write_bytes(payload)
    os.utime(first, (1, 1))
    os.utime(second, (2_000_000_000, 2_000_000_000))
    first_pin = ingest_common.content_sha256_pin(first)
    second_pin = ingest_common.content_sha256_pin(second)
    check(
        "pair: content source pin ignores path and mtime",
        first_pin == second_pin and first_pin.startswith("sha256:"),
    )
    second.write_bytes(payload + b"changed")
    check(
        "pair: content source pin changes with bytes",
        ingest_common.content_sha256_pin(second) != first_pin,
    )


def test_cap(d: Path) -> None:
    # cap 3: both anchored (a, b) + the frontier page with MOST inbound (e: 2)
    ext_nodes, _edges, stats = run_layer(d, cap=3)
    nlab = {n["id"] for n in ext_nodes if n["db"] == "nlab"}
    check("cap: anchored first, then frontier by inbound",
          nlab == {"xref:nlab:a", "xref:nlab:b", "xref:nlab:e"}, f"got {nlab}")
    check("cap: dropped count recorded", stats["capped"] == {"nlab": 2},
          f"got {stats['capped']}")


def test_snippet_guard(d: Path) -> None:
    ext_nodes, _edges, _stats = run_layer(d)
    a = next(n for n in ext_nodes if n["id"] == "xref:nlab:a")
    m = next(n for n in ext_nodes if n["id"] == "xref:mathworld:M1")
    check("snippet: license-ok db keeps snippet+license",
          a.get("snippet") == "idea of a" and "attribution" in a["snippet_license"])
    check("snippet: no-content db stripped",
          "snippet" not in m and "snippet_license" not in m)


def test_links_edges(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    pl = {(e["src"], e["dst"]): e for e in edges
          if e["kind"] == "links" and not e["evidence"].get("projected")}
    check("links: page edges between minted nodes only",
          ("xref:nlab:c", "xref:nlab:d") not in pl)
    check("links: (src,dst) deduped to best context",
          pl[("xref:nlab:a", "xref:nlab:b")]["evidence"]["context"] == "statement")
    e = pl[("xref:nlab:a", "xref:nlab:b")]
    check("links: provenance shape", e["provenance"]["source"] == "nlab"
          and e["provenance"]["method"] == "internal_link"
          and e["confidence"] == "high")
    check("links: page-edge count", stats["links_page"] == 6,
          f"got {stats['links_page']}")   # a>b b>a b>c a>e b>e b>f


def test_projection(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    proj = [e for e in edges if e["kind"] == "links"
            and e["evidence"].get("projected")]
    pairs = {(e["src"], e["dst"]) for e in proj}
    # a anchors Q1 (xref), b anchors Q2 (qid): a->b projects Q1->Q2 once
    # (two duplicate page links), b->a projects Q2->Q1
    check("proj: both directions, deduped on (src,dst,via)",
          pairs == {("Q1", "Q2"), ("Q2", "Q1")} and len(proj) == 2,
          f"got {sorted(pairs)} ({len(proj)} edges)")
    e = next(e for e in proj if e["src"] == "Q1")
    check("proj: evidence carries via + page pair",
          e["evidence"] == {"projected": True, "via": "nlab",
                            "src_page": "a", "dst_page": "b"}
          and e["confidence"] == "medium")
    check("proj: stats", stats["links_projected"] == 2)


def test_qid_xref(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    qx = {(e["src"], e["dst"]) for e in edges if e["kind"] == "xref"}
    check("qid-xref: minted for qid pages lacking one",
          qx == {("Q2", "xref:nlab:b"), ("Q1", "xref:mathworld:M1")},
          f"got {sorted(qx)}")
    check("qid-xref: stats", stats["xref_from_page_qid"] == 2)
    # a pre-existing pair suppresses the mint
    _n2, edges2, _s2 = run_layer(d, xref_pairs={("Q1", "xref:nlab:a"),
                                                ("Q2", "xref:nlab:b")})
    qx2 = {(e["src"], e["dst"]) for e in edges2 if e["kind"] == "xref"}
    check("qid-xref: existing pair not duplicated",
          qx2 == {("Q1", "xref:mathworld:M1")}, f"got {sorted(qx2)}")


def _edge(src, dst, kind, source, conf="high", ev=None):
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": source, "method": "t", "pin": "2026-01-01"},
            "confidence": conf, "evidence": ev or {}}


def facet_fixture():
    nodes = [
        {"id": "Q1", "type": "concept", "label": "one", "slug": "One",
         "article_annotations": {"total": 1, "formalized": 1},
         "display": {"status": "formalized"}},
        {"id": "Q2", "type": "concept", "label": "two",
         "display": {"status": "partial"}},
        {"id": "decl:Mathlib:Foo", "type": "decl", "label": "Foo",
         "module": "Mathlib.A.B"},
        {"id": "decl:Mathlib:Bar", "type": "decl", "label": "Bar"},
        {"id": "xref:nlab:a", "type": "ext", "db": "nlab", "label": "a",
         "url": "u", "snippet": "s", "snippet_license": "nLab"},
        {"id": "xref:mathworld:M1", "type": "ext", "db": "mathworld",
         "label": "M1", "url": "u"},
        {"id": "lit:1234.5678", "type": "literature", "label": "Paper",
         "arxiv_id": "1234.5678"},
        {"id": "lit:1234.5678#thm1", "type": "literature", "label": "Paper",
         "arxiv_id": "1234.5678", "ref": "thm1"},
        {"id": "path:Mathlib", "type": "container", "label": "Mathlib"},
    ]
    edges = [
        _edge("Q1", "xref:nlab:a", "xref", "nlab", ev={"value": "a"}),
        _edge("decl:Mathlib:Foo", "xref:stacks:0001", "xref", "stacks",
              ev={"value": "0001"}),
        _edge("Q1", "lit:x", "cites", "theoremgraph"),
        _edge("decl:Mathlib:Bar", "lit:x", "matches", "theoremgraph"),
        _edge("Q1", "decl:Mathlib:Foo", "formalizes", "mathlib",
              ev={"match_kind": "exact", "module": "Mathlib.A.B"}),
        _edge("Q1", "path:Mathlib", "formalizes", "mathlib",
              ev={"match_kind": "field"}, conf="medium"),
    ]
    tag_rows = [{"decl": "Foo", "db": "wikidata", "tag": "Q1"},
                {"decl": "Foo", "db": "stacks", "tag": "0001"},
                {"decl": "Bar", "db": "kerodon", "tag": "000T"}]
    return nodes, edges, tag_rows


def test_facets() -> None:
    nodes, edges, tag_rows = facet_fixture()
    bc.apply_facets(nodes, edges, tag_rows)
    f = {n["id"]: n.get("f", 0) for n in nodes}
    B = bc
    # bits 0-2 PROPAGATE from tagged decls to the concepts they formalize
    # (Foo carries @[wikidata]+@[stacks]) — otherwise the documented filter
    # masks (f=1, f=17) are unsatisfiable on the concept-bearing label index
    check("f: concept bits (xref+formalized+article+lit+nlab+propagated tags)",
          f["Q1"] == B.F_ANY_XREF | B.F_FORMALIZED | B.F_ARTICLE
          | B.F_LITERATURE | B.F_DB_BIT["nlab"]
          | B.F_GOLD_WIKIDATA | B.F_STACKS_ATTR, f"got {f['Q1']}")
    check("f: partial concept", f["Q2"] == B.F_PARTIAL)
    check("f: gold+stacks decl",
          f["decl:Mathlib:Foo"] == B.F_GOLD_WIKIDATA | B.F_STACKS_ATTR
          | B.F_ANY_XREF | B.F_DB_BIT["stacks"], f"got {f['decl:Mathlib:Foo']}")
    check("f: kerodon+matches decl",
          f["decl:Mathlib:Bar"] == B.F_KERODON_ATTR | B.F_LITERATURE)
    check("f: ext with snippet (xref-touched)",
          f["xref:nlab:a"] == B.F_EXT | B.F_DB_BIT["nlab"]
          | B.F_HAS_SNIPPET | B.F_ANY_XREF, f"got {f['xref:nlab:a']}")
    check("f: ext no snippet, no xref touch",
          f["xref:mathworld:M1"] == B.F_EXT | B.F_DB_BIT["mathworld"])
    check("f: lit paper carries the literature bit natively",
          f["lit:1234.5678"] == B.F_LITERATURE)
    check("f: lit statement stays bare", f["lit:1234.5678#thm1"] == 0)
    check("f: zero omitted", "f" not in nodes[-1])


def test_units() -> None:
    nodes, edges, _tags = facet_fixture()
    bc.assemble_units(nodes, edges, {"Q1": "a description"}, REG)
    u = nodes[0]["unit"]
    check("unit: identity + description",
          u["qid"] == "Q1" and u["label"] == "one"
          and u["description"] == "a description")
    check("unit: article from slug+annotations",
          u["article"] == {"slug": "One",
                           "annotations": {"total": 1, "formalized": 1}})
    check("unit: decls from formalizes edges",
          u["decls"] == [{"name": "Foo", "module": "Mathlib.A.B",
                          "match_kind": "exact", "confidence": "high"}],
          f"got {u['decls']}")
    check("unit: containers from formalizes->path",
          u["containers"] == ["path:Mathlib"])
    # the minted ext node's adapter-encoded url wins over the registry
    # template join (ids can carry spaces the template can't encode)
    check("unit: xrefs w/ ext label + ext-node url preferred",
          u["xrefs"] == {"nlab": [{"id": "a", "label": "a", "url": "u"}]},
          f"got {u['xrefs']}")
    u2 = nodes[1]["unit"]
    check("unit: every concept gets one (empty members allowed)",
          u2 == {"qid": "Q2", "label": "two", "decls": [],
                 "containers": [], "xrefs": {}})
    check("unit: non-concepts untouched",
          all("unit" not in n for n in nodes if n["type"] != "concept"))


def test_literature_layer() -> None:
    lit_title = {
        "lit:1234.5678#thm1": "Paper A", "lit:1234.5678#thm2": "Paper A",
        "lit:2001.00001": "Paper B",              # empty-ref statement = paper
        "lit:math/0211261#5.2": "Paper C",
    }
    lic = {"1234.5678": True, "math/0211261": False}
    with tempfile.TemporaryDirectory() as td:
        cit = Path(td) / "arxiv_citations.jsonl"
        with cit.open("w") as f:
            for r in [{"_meta": {"db": "openalex"}},
                      {"src": "1234.5678", "dst": "2001.00001"},
                      {"src": "1234.5678", "dst": "2001.00001"},   # dup
                      {"src": "2001.00001", "dst": "math/0211261"},
                      {"src": "1234.5678", "dst": "9999.99999"},   # not ours
                      {"src": "1234.5678", "dst": "1234.5678"}]:   # self
                f.write(json.dumps(r) + "\n")
        nodes, edges, stats = bc.literature_layer(lit_title, lic, cit,
                                                  "2026-01-01")
    ids = {n["id"] for n in nodes}
    check("lit: papers minted per arXiv id (empty-ref id NOT re-minted)",
          ids == {"lit:1234.5678", "lit:math/0211261"}, f"got {sorted(ids)}")
    p = next(n for n in nodes if n["id"] == "lit:1234.5678")
    check("lit: paper payload shape",
          p == {"id": "lit:1234.5678", "type": "literature",
                "label": "Paper A", "arxiv_id": "1234.5678",
                "license_open": True}, f"got {p}")
    cont = [(e["src"], e["dst"]) for e in edges if e["kind"] == "contains"]
    check("lit: contains paper→statement (no empty-ref self-containment)",
          set(cont) == {("lit:1234.5678", "lit:1234.5678#thm1"),
                        ("lit:1234.5678", "lit:1234.5678#thm2"),
                        ("lit:math/0211261", "lit:math/0211261#5.2")}
          and len(cont) == 3, f"got {sorted(cont)}")
    c = next(e for e in edges if e["kind"] == "contains")
    check("lit: contains provenance (id-prefix derivation, statement pin)",
          c["provenance"] == {"source": "theoremgraph",
                              "method": "arxiv-id prefix (paper→statement)",
                              "pin": "2026-01-01"})
    links = [e for e in edges if e["kind"] == "links"]
    got = {(e["src"], e["dst"]) for e in links}
    check("lit: bibliography links deduped, both-endpoints-ours, no self",
          got == {("lit:1234.5678", "lit:2001.00001"),
                  ("lit:2001.00001", "lit:math/0211261")}
          and len(links) == 2, f"got {sorted(got)}")
    e = next(e for e in links if e["src"] == "lit:1234.5678")
    check("lit: links edge shape",
          e["provenance"]["source"] == "openalex"
          and e["provenance"]["method"] == "referenced_works"
          and e["confidence"] == "high"
          and e["evidence"] == {"context": "bibliography"})
    check("lit: stats",
          stats == {"papers": 3, "papers_new": 2, "contains": 3,
                    "citations": 2, "citation_rows_dropped": 3},
          f"got {stats}")
    # absence degrade: missing citations file ⇒ ZERO links edges; papers +
    # contains still mint (they derive from the statement layer alone)
    nodes2, edges2, stats2 = bc.literature_layer(
        lit_title, lic, Path("/nonexistent/arxiv_citations.jsonl"),
        "2026-01-01")
    check("lit: absent citations file degrades to zero links",
          {n["id"] for n in nodes2} == ids
          and not [e for e in edges2 if e["kind"] == "links"]
          and len([e for e in edges2 if e["kind"] == "contains"]) == 3
          and stats2["citations"] == 0
          and stats2["citation_rows_dropped"] == 0, f"got {stats2}")


def test_split_writer() -> None:
    edges = [
        _edge("path:M", "decl:M:X", "contains", "theoremgraph"),
        _edge("Q1", "decl:M:X", "formalizes", "mathlib"),
        _edge("xref:nlab:a", "xref:nlab:b", "links", "nlab",
              ev={"context": "body"}),
        _edge("Q1", "Q2", "links", "nlab",
              ev={"projected": True, "via": "nlab"}),
    ]
    meta = {"schema": "brain/SCHEMA.md",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "counts": {"edges": {"contains": 1, "formalizes": 1, "links": 2}},
            "notes": {"links_page_edges": 1, "links_projected_edges": 1}}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "edges.jsonl"
        out_links = Path(td) / "edges_links.jsonl"
        n = bc.write_edges(edges, meta, out=out, out_links=out_links)
        check("split: row counts returned", n == {"main": 2, "links": 2},
              f"got {n}")
        main_lines = out.read_text().splitlines()
        check("split: main meta is the FULL build meta (byte-compat with the "
              "pre-split file)", json.loads(main_lines[0])["_meta"] == meta)
        check("split: main file excludes links, order preserved",
              [json.loads(l)["kind"] for l in main_lines[1:]]
              == ["contains", "formalizes"])
        links_lines = out_links.read_text().splitlines()
        lm = json.loads(links_lines[0])["_meta"]
        check("split: links meta (own counts + provenance of the split)",
              lm["split_from"] == "edges.jsonl"
              and lm["generated_at"] == meta["generated_at"]
              and lm["counts"] == {"edges": {"links": 2},
                                   "page_level": 1, "projected": 1},
              f"got {lm}")
        rows = [json.loads(l) for l in links_lines[1:]]
        check("split: links file has only links rows, order preserved",
              [r["kind"] for r in rows] == ["links", "links"]
              and rows[0]["src"] == "xref:nlab:a" and rows[1]["src"] == "Q1")
        # zero-links build still (re)writes the file — no stale rows survive
        n0 = bc.write_edges(edges[:2], meta, out=out, out_links=out_links)
        check("split: zero-links build rewrites an empty links file",
              n0 == {"main": 2, "links": 0}
              and len(out_links.read_text().splitlines()) == 1)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_external(d)
        test_env_override()
        test_loading(d)
        test_external_pair_publication(d)
        test_content_pin_ignores_path_and_mtime(d)
        test_minting(d)
        test_cap(d)
        test_snippet_guard(d)
        test_links_edges(d)
        test_projection(d)
        test_qid_xref(d)
    test_facets()
    test_units()
    test_literature_layer()
    test_split_writer()
    print(f"\n{'FAIL: ' + ', '.join(FAILURES) if FAILURES else 'all green'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
