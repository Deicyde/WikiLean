#!/usr/bin/env python3
"""Stage 9a: the ``web-html/3`` renderer, on a synthetic page.

docs/PIPELINE.md sections 3 and 8. No article text: the page below is written here and
holds every shape the renderer has a rule for. The batch-1 proof — all 128 rendered
sources of the ``wpm3`` sourceset re-rendered from their stored ``body.html``, every
view byte for byte — is an operator run over data this repository does not hold, and
its result is in the report.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for _candidate in (REPO / "site", HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anchor_resolve  # noqa: E402
import locate_text  # noqa: E402
import render_web as render  # noqa: E402
import vocab  # noqa: E402

PAGE = """<div class="mw-parser-output">
<p>A <b>widget space</b> is a set with a widget structure.<sup class="reference"\
><a href="#n1">[1]</a></sup>
It is studied in widget&#160;theory.</p>
<h2 id="Definition">Definition<span class="mw-editsection">[<a href="/e">edit</a>]\
</span></h2>
<p><b>Definition.</b> A map <span class="mwe-math-element"><math \
alttext="{\\displaystyle f\\colon X\\to Y}"><semantics><mrow><mi>f</mi></mrow>\
<annotation encoding="application/x-tex">f\\colon X\\to Y</annotation></semantics>\
</math></span> is <b>widgetwise continuous</b> when it preserves widgets.</p>
<dl><dd><span class="mwe-math-element mwe-math-element-block"><math display="block" \
alttext="{\\displaystyle \\sum a = 1}"><semantics><annotation \
encoding="application/x-tex">\\sum a = 1</annotation></semantics></math></span></dd></dl>
<h2 id="Results">Results</h2>
<div class="math_theorem"><span class="theorem-name">Theorem 1 (Widget lemma)</span>\
&#160;&#8212;&#160; Every widget space is a <i>space</i>.</div>
<ul><li>First item with <span class="texhtml"><i>n</i></span> inside.</li>\
<li>Second item.</li></ul>
<table><tr><td>Left</td><td>Right</td></tr></table>
<style>.x{color:red}</style><script>var a = "<p>not text</p>";</script>
<ol class="references"><li>A reference.</li></ol>
</div>
"""


def rendering():
    return render.render(render.canonical_body(PAGE.encode("utf-8")))


class TestCanonicalBody(unittest.TestCase):
    def test_parse_response_both_format_versions(self):
        two = json.dumps({"parse": {"text": "<p>hi</p>"}}).encode("utf-8")
        one = json.dumps({"parse": {"text": {"*": "<p>hi</p>"}}}).encode("utf-8")
        self.assertEqual(render.canonical_body(two), "<p>hi</p>")
        self.assertEqual(render.canonical_body(one), "<p>hi</p>")

    def test_plain_html_and_a_byte_order_mark(self):
        self.assertEqual(render.canonical_body(b"\xef\xbb\xbf<p>hi</p>"), "<p>hi</p>")

    def test_a_lone_surrogate_is_refused(self):
        with self.assertRaises(render.RenderError):
            render.canonical_body("<p>\ud800</p>")

    def test_comments_are_delimited_as_the_tokenizer_delimits_them(self):
        cases = {
            "a<!-- b -->c": "ac",
            "a<!-- b --!>c": "ac",
            "a<!-->c": "ac",
            "a<!--->c": "ac",
            "a<!-- unterminated": "a",
            '<img alt="<!--">x': '<img alt="<!--">x',
            "<style>/* <!-- */</style>": "<style>/* <!-- */</style>",
        }
        for body, want in cases.items():
            with self.subTest(body=body):
                self.assertEqual(render.strip_comments(body), want)


class TestLayerIsAnchorResolves(unittest.TestCase):
    def test_the_layer_is_site_anchor_resolve_text_layer(self):
        body = render.canonical_body(PAGE.encode("utf-8"))
        self.assertEqual(rendering().layer, anchor_resolve.text_layer(body)[0])

    def test_script_style_and_nbsp(self):
        layer = rendering().layer
        self.assertNotIn("not text", layer)
        self.assertNotIn("color:red", layer)
        self.assertIn("widget theory", layer)        # &#160; is White_Space, collapsed
        self.assertNotIn(" ", layer)

    def test_the_citation_marker_is_in_the_layer_and_not_in_the_reading_copy(self):
        out = rendering()
        self.assertIn("[1]", out.layer)
        self.assertNotIn("[1]", out.text)

    def test_a_body_the_two_readers_disagree_about_is_refused(self):
        with self.assertRaises(render.RenderError) as caught:
            render._Layer("abc", "abd")
        self.assertIn("do not agree", str(caught.exception))
        with self.assertRaises(render.RenderError):
            render._Layer("ab", "abc")


class TestViews(unittest.TestCase):
    def setUp(self):
        self.r = rendering()

    def test_formulas_carry_their_alttext_and_span(self):
        alts = [f["alttext"] for f in self.r.formulas]
        self.assertEqual(alts, ["{\\displaystyle f\\colon X\\to Y}",
                                "{\\displaystyle \\sum a = 1}"])
        for formula in self.r.formulas:
            a, b = formula["span"]
            self.assertLess(a, b)
            self.assertLessEqual(b, len(self.r.layer))
        self.assertEqual(self.r.formulas, sorted(self.r.formulas, key=lambda f: f["span"]))

    def test_the_head_replaces_each_formula_with_its_token(self):
        self.assertIn(locate_text.formula_token("{\\displaystyle \\sum a = 1}"), self.r.head)
        self.assertEqual(self.r.head, locate_text.HeadMap(self.r.layer, self.r.formulas).head)

    def test_sections_tile_the_layer_and_the_lead_is_first(self):
        spans = [s["span"] for s in self.r.sections]
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(self.r.layer))
        for before, after in zip(spans, spans[1:]):
            self.assertEqual(before[1], after[0])
        self.assertEqual(self.r.sections[0]["path"], [])
        self.assertEqual([s["path"] for s in self.r.sections[1:]],
                         [["Definition"], ["Results"]])
        # the edit link is no part of a title
        self.assertNotIn("edit", "".join(t for s in self.r.sections for t in s["path"]))

    def test_the_heading_fragment_is_its_own_id(self):
        self.assertEqual([s["fragment"] for s in self.r.sections],
                         [None, "Definition", "Results"])

    def test_blocks_name_their_kind_and_count_their_formulas(self):
        kinds = sorted({p["block"] for p in self.r.paragraphs})
        self.assertEqual(kinds, ["dd", "li", "math", "p", "theorem_box"])
        display = [p for p in self.r.paragraphs if p["block"] == "math"]
        self.assertEqual(len(display), 1)
        self.assertTrue(display[0]["math"])
        texhtml = [p for p in self.r.paragraphs if p["texhtml"]]
        self.assertEqual([p["block"] for p in texhtml], ["li"])

    def test_a_dropped_region_is_no_block(self):
        # the reference list is dropped, so its li is not a block
        for block in self.r.paragraphs:
            quoted = self.r.layer[block["span"][0]:block["span"][1]]
            self.assertNotIn("A reference.", quoted)

    def test_the_three_finders(self):
        rows = {(f["finder"], f["term"]) for f in self.r.finders}
        self.assertIn(("bold_definition", "widget space"), rows)
        self.assertIn(("bold_definition", "widgetwise continuous"), rows)
        self.assertIn(("theorem_box", "Theorem 1 (Widget lemma)"), rows)
        self.assertIn((vocab.FINDER_LEAD, None), rows)
        self.assertEqual({f["finder"] for f in self.r.finders},
                         set(vocab.DETERMINISTIC_FINDERS))
        # a label-only bold ("Definition.") names no term
        self.assertNotIn("Definition.", {f["term"] for f in self.r.finders})

    def test_every_finder_starts_at_a_word_start_of_the_head(self):
        head_map = self.r.head_map()
        for row in self.r.finders:
            locate_text.check_start(self.r.head, head_map.to_head(row["start"]))

    def test_the_reading_copy(self):
        text = self.r.text
        self.assertIn("## Definition", text)
        self.assertIn("$\\sum a = 1$", text)
        self.assertIn("- First item", text)
        self.assertIn("Left | Right", text)
        self.assertNotIn("A reference.", text)
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_the_layer_map_round_trips_text_and_formulas(self):
        mapped = render.LayerMap(self.r.segments)
        for t in range(len(self.r.text)):
            at = mapped.text_to_layer(t)
            if at is None:
                continue
            seg = [s for s in self.r.segments if s[0] <= t < s[1]][0]
            if seg[4] == render.SEGMENT_TEXT:
                self.assertEqual(self.r.text[t], self.r.layer[at])
        for formula in self.r.formulas:
            at = mapped.layer_to_text(formula["span"][0])
            self.assertIsNotNone(at)
            self.assertEqual(self.r.text[at], "$")

    def test_the_counts(self):
        counts = self.r.counts
        self.assertEqual(counts["layer_chars"], len(self.r.layer))
        self.assertEqual(counts["text_chars"], len(self.r.text))
        self.assertEqual(counts["formulas"], len(self.r.formulas))
        self.assertEqual(counts["sections"], len(self.r.sections))
        self.assertEqual(counts["paragraphs"], len(self.r.paragraphs))
        self.assertEqual(counts["texhtml"], 1)
        self.assertEqual(counts["finders"],
                         {"theorem_box": 1, "bold_definition": 2, "lead": 1})


class TestBoldLabels(unittest.TestCase):
    def test_labels(self):
        for text in ("Proof.", "proof", "Example 2:", "Definition:", "Case 2:",
                     "Step 1a.", "Example (iii)", "Solution:", "Problem 3.",
                     "Proof of the lemma:", "Proof (sketch)."):
            self.assertTrue(vocab.is_bold_label(text), text)

    def test_not_labels(self):
        for text in ("widget space", "Axiom A", "Problem P", "property (T)",
                     "Theorem (Widget lemma).", "Solution", "Hausdorff space"):
            self.assertFalse(vocab.is_bold_label(text), text)

    def test_a_short_bold_is_a_symbol_and_no_term(self):
        self.assertFalse(render.is_term("R"))
        self.assertFalse(render.is_term("1'"))
        self.assertTrue(render.is_term("ring"))


class TestTheRendererIdIsFrozen(unittest.TestCase):
    """What a change here must change the id for (docs/PIPELINE.md stage 9a)."""

    def test_the_id(self):
        self.assertEqual(render.RENDERER, "web-html/3")
        self.assertEqual(render.RENDERER, vocab.RENDERER_WEB_HTML)

    def test_the_label_rule_fingerprint(self):
        # the two label sets and the rule decide which paragraphs make a passage, so a
        # change to either moves finders.json under an unchanged renderer id
        self.assertEqual(len(vocab.BOLD_LABELS), 38)
        self.assertEqual(len(vocab.BOLD_LABELS_PUNCTUATED), 20)
        self.assertEqual(sorted(vocab.BOLD_LABELS & vocab.BOLD_LABELS_PUNCTUATED), [])

    def test_the_files_a_rendering_writes(self):
        self.assertEqual(render.RENDER_FILES,
                         ("body.html", "layer.txt", "formulas.json", "head.txt", "text.txt",
                          "layer_map.json", "sections.json", "paragraphs.json",
                          "finders.json", "render.json"))
        self.assertEqual(set(rendering().files()), set(render.RENDER_FILES))

    def test_the_record_carries_every_other_view_s_sha(self):
        out = rendering()
        record = out.record()
        views = out.files()
        self.assertEqual(set(record["views"]), set(render.VIEW_FILES))
        for name, sha in record["views"].items():
            self.assertEqual(sha, render.sha256_text(views[name]))
        self.assertEqual(record["layer_sha256"], render.sha256_text(out.layer))


class TestWriting(unittest.TestCase):
    def test_a_rendering_is_written_and_re_rendered_unchanged(self):
        out = rendering()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "enwiki-1-2"
            render.write_rendering(directory, out)
            self.assertEqual(render.view_differences(directory, out), ([], []))
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            again = render.render_directory(directory)
            self.assertEqual(again.record(), out.record())
            after = {p.name: p.read_bytes() for p in directory.iterdir()}
            self.assertEqual(before, after)

    def test_a_view_written_by_another_rule_is_named(self):
        out = rendering()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "enwiki-1-2"
            render.write_rendering(directory, out)
            (directory / "layer.txt").write_text("other", encoding="utf-8")
            (directory / "finders.json").unlink()
            missing, differing = render.view_differences(directory, out)
            self.assertEqual(missing, ["finders.json"])
            self.assertEqual(differing, ["layer.txt"])

    def test_a_layer_map_of_another_renderer_is_refused(self):
        out = rendering()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "enwiki-1-2"
            render.write_rendering(directory, out)
            self.assertIsInstance(render.LayerMap.read(directory), render.LayerMap)
            data = json.loads((directory / "layer_map.json").read_text(encoding="utf-8"))
            data["renderer"] = "web-html/2"
            (directory / "layer_map.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(render.RenderError):
                render.LayerMap.read(directory)


class TestDeterminism(unittest.TestCase):
    def test_the_same_body_renders_to_the_same_bytes(self):
        self.assertEqual(rendering().files(), rendering().files())

    def test_a_body_nested_too_deep_is_refused(self):
        body = "<div>" * (render.MAX_DEPTH + 2) + "x"
        with self.assertRaises(render.RenderError):
            render.render(body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
