#!/usr/bin/env python3
"""Write scripts/articles/fixtures/reader-html-statements.json, the marked revision of the
synthetic cut's first article.

A statement set seals each statement's span on the pinned layer, so `--reader-html` checks out
against a published cut exactly as it does against a record set (docs/ARTICLES.md section 4a,
"The marked revision of a cut"). This writes that side file for `enwiki-1005-3006`, the
article `scripts/pipeline/fixtures/make_statement_fixture.py` cuts: one paragraph a statement,
each published statement wrapped in its mark with `data-status="unchecked"`, and the one
`expository` statement — which the store does not publish, because no locate run will ever
search it — left unmarked.

Everything here is invented, as the cut it marks is: no article text, no real page.

Run: python3 scripts/articles/fixtures/make_reader_fixture.py [--out <file>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _candidate in (ROOT / "scripts" / "pipeline" / "fixtures",):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import make_statement_fixture as cut  # noqa: E402

SOURCE_ID = "enwiki-1005-3006"
SCHEMA = "wikilean.articles.reader-html/1"
# The status every mark of a published cut carries: the passage's own outcome, which the
# Worker's reader styles exactly as it styles the summary status not_checked.
UNCHECKED = "unchecked"


def body() -> str:
    """The article's revision: one paragraph a statement, each published one marked.

    web-html/3 reads a body's text with every run of whitespace collapsed to one space, so the
    paragraphs below read back as exactly the layer the cut was made on (the statement texts
    joined by one space) and each mark covers exactly its sealed span.
    """
    sealed = {row["item"]: row for row in cut.passages(SOURCE_ID)}
    lines = []
    for item, row in zip(sealed, cut.CUTS[SOURCE_ID]):
        hex_ = item.removeprefix("In-text #")
        text = row["text"]
        if row["scope"] != "formal_math":
            lines.append(f"<p>{text}</p>")
            continue
        lines.append(f'<p><mark class="atl" data-p="{hex_}" data-status="{UNCHECKED}" '
                     f'id="p-{hex_}">{text}</mark></p>')
    return '<div class="mw-parser-output">\n' + "\n".join(lines) + "\n</div>"


def document() -> dict:
    source = next(row for row in cut.sources() if row["source_id"] == SOURCE_ID)
    html = body()
    return {"schema": SCHEMA, "articles": {str(source["pageid"]): {
        "source_id": SOURCE_ID, "revision": source["revision"],
        "html_sha256": source["html_sha256"], "layer_sha256": source["layer_sha256"],
        "renderer": source["renderer"], "html": html, "unplaced": [],
    }}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "reader-html-statements.json")
    args = parser.parse_args(argv)
    args.out.write_text(json.dumps(document(), ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n", encoding="utf-8")
    print(f"{args.out}: {SOURCE_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
