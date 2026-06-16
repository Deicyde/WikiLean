// Pure-function tests for the @[wikidata] review tool's deterministic core:
// unified-diff parsing (parseWikidataTags) and inline-comment-body construction
// (buildReviewCommentBody). No network — the GitHub fetch/post paths are
// exercised against a real PR via the running Worker.
import { describe, it, expect } from "vitest";
import { parseWikidataTags, buildReviewCommentBody, cleanLead, extractDeclName } from "../src/review.js";

describe("parseWikidataTags", () => {
  it("finds an added @[wikidata] tag with its new-file line number", () => {
    const diff = [
      "diff --git a/Mathlib/Foo.lean b/Mathlib/Foo.lean",
      "index 111..222 100644",
      "--- a/Mathlib/Foo.lean",
      "+++ b/Mathlib/Foo.lean",
      "@@ -40,3 +40,4 @@ namespace Foo",
      " /-- doc -/",
      "+@[wikidata Q167]",
      " def pi : ℝ := 3",
      " ",
    ].join("\n");
    const tags = parseWikidataTags(diff);
    expect(tags).toHaveLength(1);
    expect(tags[0].qid).toBe("Q167");
    expect(tags[0].file).toBe("Mathlib/Foo.lean");
    // context line 40, doc at 40, tag added at 41
    expect(tags[0].line).toBe(41);
    expect(tags[0].hunk[0]).toBe("@[wikidata Q167]");
  });

  it("tracks new-file lines across removed lines and multiple hunks", () => {
    const diff = [
      "+++ b/Mathlib/Bar.lean",
      "@@ -10,4 +10,4 @@",
      " a",
      "-old",
      "+@[wikidata Q42]",
      " b",
      "@@ -100,2 +100,3 @@",
      " x",
      "+@[stacks 09GA, wikidata Q999]",
      " y",
    ].join("\n");
    const tags = parseWikidataTags(diff);
    expect(tags.map((t) => [t.qid, t.line])).toEqual([
      ["Q42", 11], // line 10 = "a", removed "old" doesn't advance, +tag at 11
      ["Q999", 101], // hunk2: 100 = "x", +tag at 101
    ]);
  });

  it("ignores wikidata mentions on context/removed lines", () => {
    const diff = [
      "+++ b/Mathlib/Baz.lean",
      "@@ -1,3 +1,3 @@",
      " -- see @[wikidata Q1] (a comment, not added)",
      "-@[wikidata Q2]",
      "+def real := 1",
    ].join("\n");
    expect(parseWikidataTags(diff)).toHaveLength(0);
  });
});

describe("buildReviewCommentBody", () => {
  it("uses the traffic-light label, blockquotes the verbatim note, and embeds the marker", () => {
    const body = buildReviewCommentBody("Q652446", "reject", "want the class, not the structure");
    expect(body).toContain("🔴 WikiLean reviewer note (reject)");
    expect(body).toContain("> want the class, not the structure");
    expect(body).toContain("<!-- wikilean-review:Q652446 -->");
    expect(body).toContain("https://www.wikidata.org/wiki/Q652446");
  });

  it("blockquotes multi-line notes verbatim", () => {
    const body = buildReviewCommentBody("Q1", "revise", "line one\nline two");
    expect(body).toContain("> line one\n> line two");
  });

  it("handles a decision with no note", () => {
    const body = buildReviewCommentBody("Q1", "approve", "");
    expect(body).toContain("🟢 WikiLean reviewer note (approve)");
    expect(body).toContain("_(no note)_");
  });

  it("annotates a status change with 'changed from'", () => {
    const body = buildReviewCommentBody("Q1", "reject", "disagree", "approve");
    expect(body).toContain("🔴 WikiLean reviewer note (reject) — changed from 🟢 approve");
    expect(body).toContain("> disagree");
  });

  it("omits 'changed from' when status is unchanged or had no prior", () => {
    expect(buildReviewCommentBody("Q1", "reject", "x", "reject")).not.toContain("changed from");
    expect(buildReviewCommentBody("Q1", "reject", "x", "")).not.toContain("changed from");
  });
});

describe("extractDeclName", () => {
  it("reads a fully-qualified name declared inline", () => {
    const lines = ["@[wikidata Q123]", "class Module.Projective (R : Type*) [Semiring R] : Prop where", "  out : True"];
    expect(extractDeclName(lines, "Q123")).toBe("Module.Projective");
  });
  it("prepends an enclosing namespace to a short name", () => {
    const lines = ["namespace Order", "", "@[wikidata Q42]", "def cof (o : Ordinal) : Ordinal := o", "", "end Order"];
    expect(extractDeclName(lines, "Q42")).toBe("Order.cof");
  });
  it("skips bare modifiers and stacked attributes on their own lines", () => {
    const lines = ["@[wikidata Q7]", "@[simp]", "noncomputable", "def foo : Nat := 0"];
    expect(extractDeclName(lines, "Q7")).toBe("foo");
  });
  it("keeps subscripts and primes in the name", () => {
    const lines = ["@[wikidata Q9]", "def jacobiTheta₂' (z τ : ℂ) : ℂ := z"];
    expect(extractDeclName(lines, "Q9")).toBe("jacobiTheta₂'");
  });
  it("returns null for an anonymous instance", () => {
    const lines = ["@[wikidata Q5]", "instance : Foo Nat where", "  bar := 0"];
    expect(extractDeclName(lines, "Q5")).toBeNull();
  });
  it("doesn't double-prefix when the signature already carries the namespace", () => {
    const lines = ["namespace A", "@[wikidata Q1]", "def A.foo : Nat := 0", "end A"];
    expect(extractDeclName(lines, "Q1")).toBe("A.foo");
  });
});

describe("cleanLead", () => {
  it("strips {\\displaystyle …} math wrappers to readable text", () => {
    const inp = "the absolute value of a real number {\\displaystyle |x|}, is the non-negative value.";
    expect(cleanLead(inp)).toBe("the absolute value of a real number |x|, is the non-negative value.");
  });
  it("handles one level of nested braces", () => {
    expect(cleanLead("for {\\displaystyle x\\in \\mathbb {R} } we have")).toContain("\\mathbb {R}");
    expect(cleanLead("for {\\displaystyle x\\in \\mathbb {R} } we have")).not.toContain("\\displaystyle");
  });
  it("passes through plain text and null", () => {
    expect(cleanLead("just prose, no math.")).toBe("just prose, no math.");
    expect(cleanLead(null)).toBe(null);
  });
});
