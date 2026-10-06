#!/usr/bin/env python3
"""Offline native integration: WikiLean copied SQLite + FCA + Autoform attachments.

Uses an independent synthetic Git consumer, the installed Autoform CLI, the real
FCA checker/discovery, and WikiLean's real route over its hermetic D1 fixture.
Nothing is pushed, deployed, downloaded, or proved. Output is kept for inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.ingest.companion import import_companions, iter_annotations


def run(args, *, cwd, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=120, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}\n{result.stderr}\n{result.stdout}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autoform", required=True, help="installed Autoform CLI with native companions")
    parser.add_argument("--node", default="node", help="Node 22 executable (wiki/npm ci must already be installed)")
    parser.add_argument("--output", type=Path, required=True, help="new directory to retain the fixture and index")
    args = parser.parse_args()
    from formal_companion_annotations.companion import check
    from formal_companion_annotations.discovery import collect

    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        parser.error("output already exists; choose a new directory")
    node_version = run([args.node, "--version"], cwd=ROOT).strip()
    if not node_version.startswith("v22."):
        parser.error(f"Node 22 is required by WikiLean, found {node_version}")
    output.mkdir(parents=True)
    # Discovery reports resolved carriers (macOS /tmp is /private/tmp).
    output = output.resolve()
    consumer = output / "consumer"
    roadmap = consumer / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Synthetic integration roadmap\n", encoding="utf8")
    (roadmap / "result.md").write_text(
        "---\narticle_id: af_000000000000000000000001\ndeclaration: theorem\n"
        "lean: Consumer.result Consumer.other\n---\n\n# Synthetic integration result\n\n"
        "Two elementary test propositions. This is an offline integration fixture.\n", encoding="utf8")
    (consumer / "Consumer.lean").write_text(
        "namespace Consumer\ntheorem result : True := by trivial\n"
        "theorem other : True := by trivial\nend Consumer\n", encoding="utf8")
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git_env.update(GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")

    def git(*arguments):
        return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                    "-c", "user.name=Companion fixture", "-c", "user.email=fixture@example.invalid",
                    *arguments], cwd=consumer, env=git_env).strip()

    coverage = consumer / "blueprint/coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text("# Coverage contract\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Synthetic interoperability fixture | MAPPED | This consumer demonstrates annotation exchange only. Imported passages have not been reviewed for mathematical coverage or assigned formalization work. |\n")
    git("init", "-q", "--template=")
    git("add", "blueprint/roadmap/README.md", "blueprint/roadmap/result.md", "blueprint/coverage/README.md", "Consumer.lean")
    git("commit", "-qm", "Synthetic companion integration fixture")
    commit = git("rev-parse", "HEAD")
    autoform_output = output / "autoform"
    autoform_output.mkdir()
    article_source = f"https://raw.githubusercontent.com/example/companion-fixture/{commit}/blueprint/roadmap/result.md"
    autoform_records = [{"source": {"type": "generic", "uri": article_source},
                         "target": f"urn:lean:Consumer.{name}",
                         "extensions": {"fixture": {"native": True}}} for name in ("result", "other")]
    (autoform_output / "links.json").write_text(json.dumps(autoform_records) + "\n")
    web = output / "wiki" / "nested"
    web.mkdir(parents=True)
    native_fixture = output / "wikilean-database"
    wiki_report = json.loads(run([args.node, "--import", "tsx", "scripts/native-companion-fixture.ts", str(native_fixture)], cwd=ROOT / "wiki"))
    (web / "article.json").write_bytes((native_fixture / "links.json").read_bytes())

    # Mix all location modes, duplicate IDs and huge integers with real exports.
    # Relative references deliberately resolve against nested/modes.jsonl.
    modes = [
        {"id": "repeated", "source": {"type": "pdf", "uri": "../book.pdf"},
         "location": {"page": 2, "rect": [0.1, 0.2, 0.3, 0.4]}, "target": "../../formal.html#pdf"},
        {"id": "repeated", "source": {"type": "latex", "uri": "../book.tex"},
         "location": {"label": "thm:result"}, "target": "urn:lean:Fixture.result"},
        {"source": {"type": "html", "uri": "../book.html"},
         "location": {"fragment": "result"}, "target": "../../formal.html#html"},
        {"source": {"type": "generic", "uri": "../image.svg"},
         "location": {"custom": [1, 2, "arbitrary input"]}, "target": "urn:lean:Fixture.image",
         "extensions": {"example.org/opaque": {"integer": 2**100, "unknown": [3, 1, 2]}}},
    ]
    (web / "modes.jsonl").write_text("\n".join(json.dumps(a) for a in [*modes, modes[0]]) + "\n", encoding="utf8")
    roots = [autoform_output, output / "wiki"]
    discovered = collect(roots)
    assert not discovered.problems, discovered.problems
    assert len(discovered.records) == 12, len(discovered.records)
    assert all(not check(record.annotation).problems for record in discovered.records)
    assert all("version" not in record.annotation for record in discovered.records)

    index = output / "index"
    report = import_companions(roots, source_key="synthetic-integration", output=index)
    rows = list(iter_annotations(index))
    assert len(rows) == report["annotations"] == 12
    identity = lambda annotation: json.dumps(annotation, sort_keys=True)
    assert sorted(identity(row["annotation"]) for row in rows) == sorted(identity(r.annotation) for r in discovered.records)
    pdf_source = (output / "wiki" / "book.pdf").as_uri()
    pdf = list(iter_annotations(index, source=pdf_source))
    assert len(pdf) == 2
    assert {row["origin"]["line"] for row in pdf} == {1, 5}
    assert {row["target"] for row in pdf} == {(output / "formal.html").as_uri() + "#pdf"}
    exported = [row for row in rows if row["annotation"].get("extensions", {}).get("fixture", {}).get("native")]
    assert len(exported) == 2
    assert len(list(iter_annotations(index, target=exported[0]["target"]))) == 1
    from autoform_cli.companions import capture_annotations, write_retained_annotations
    from autoform_cli.render import render_site
    archive = output / "native-records"
    write_retained_annotations(capture_annotations(discovered), output=archive)
    article_path = roadmap / "result.md"
    article_before = article_path.read_bytes()
    (consumer / ".autoform-companions.json").write_text(json.dumps({
        "archives": [str(archive / "records.jsonl")],
        "bindings": [{"source": article_source, "article": "blueprint/roadmap/result.md",
                      "sha256": hashlib.sha256(article_before).hexdigest()}],
    }, indent=2) + "\n")
    inspected = json.loads(run([args.autoform, "companions", "inspect", str(consumer), "--json"], cwd=consumer))
    bound = [item for item in inspected["attachments"] if item["state"] == "bound-revision"]
    assert len(bound) == 2, inspected
    render_site(consumer / "blueprint", output / "autoform-view", repository_url="https://github.com/example/companion-fixture", ref=commit)
    assert article_path.read_bytes() == article_before
    from autoform_cli.markdown import site_converter
    chapter = (output / "autoform-view/roadmap/README.md").read_text()
    published = site_converter().convert(chapter)
    assert 'data-companion-state="bound-revision"' in published
    assert 'urn:lean:Consumer.result' in published
    assert str(output) not in published  # Carrier paths stay operational metadata.
    summary = {
        "passed": True, "synthetic": True, "autoform_annotations": 2,
        "wikilean_http_annotations": 5, "mode_and_duplicate_fixtures": 5,
        "sqlite_annotations": len(rows), "source_modes": ["pdf", "latex", "html", "generic"],
        "consumer_commit": commit, "database": report["database"],
        "native_sqlite_migration": wiki_report, "autoform_bound_associations": len(bound),
        "article_bytes_unchanged": article_path.read_bytes() == article_before,
        "autoform_view": str(output / "autoform-view"),
    }
    (output / "integration-result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
