// The atlas quote's formulas (src/atlas-math.ts, docs/ATLAS.md "UI"): a layer quote
// holds each formula twice, as MathML token text and as its TeX annotation; the page
// renders it once, and the in-context fragment quotes the prose only.
import { describe, expect, it } from "vitest";
import { mathHtml, passageTextHtml, quoteFragment, quoteSegments, renderQuote } from "../src/atlas-math.js";

const FA = String.fromCharCode(0x2061); // MathML's function application, as the layer carries it
const strip = (html: string) => html.replace(/<[^>]+>/g, "");

describe("quoteSegments", () => {
  it("takes the MathML tokens in front of each annotation as the formula", () => {
    const segs = quoteSegments("If w : G ~ → G {\\displaystyle w:{\\tilde {G}}\\to G} is a gadget");
    expect(segs.map((s) => s.kind)).toEqual(["text", "math", "text"]);
    expect(segs[0]).toMatchObject({ kind: "text", text: "If " });
    expect(segs[1]).toMatchObject({ kind: "math", how: "back", tokens: "w : G ~ → G", tex: "w:{\\tilde {G}}\\to G", complete: true });
    expect(segs[2]).toMatchObject({ kind: "text", text: " is a gadget" });
  });

  it("never takes a prose word that the TeX does not account for", () => {
    const segs = quoteSegments("Take W {\\displaystyle W} and W ′ {\\displaystyle W'} as widgets");
    expect(segs.filter((s) => s.kind === "math").map((s) => s.kind === "math" && s.tokens)).toEqual(["W", "W ′"]);
    expect(segs[0]).toMatchObject({ text: "Take " });
    expect(segs[2]).toMatchObject({ text: " and " });
  });

  it("matches folded letters: \\mathbb{R} is the token R, a function name keeps its application mark", () => {
    const segs = quoteSegments(`Pick u ∈ R k {\\displaystyle u\\in \\mathbb {R} ^{k}} and exp ${FA} ( t ) {\\displaystyle \\exp(t)} here`);
    const math = segs.filter((s) => s.kind === "math");
    expect(math.map((s) => s.kind === "math" && s.how)).toEqual(["back", "back"]);
    expect(segs[0]).toMatchObject({ text: "Pick " });
  });

  it("a symbol-only formula takes its own symbol and nothing more", () => {
    const segs = quoteSegments("the gadget product ∗ {\\displaystyle \\,*\\,} is compatible with + {\\displaystyle \\,+\\,}");
    expect(segs.map((s) => (s.kind === "math" ? `[${s.tokens}]` : s.text))).toEqual(["the gadget product ", "[∗]", " is compatible with ", "[+]"]);
  });

  it("an annotation the cut truncated keeps its whole tokens, matched against the TeX's head", () => {
    const segs = quoteSegments("Compare the two widgets ( R + , ∗ ) {\\displaystyle (\\mathbb", true);
    expect(segs[1]).toMatchObject({ kind: "math", how: "front", tokens: "( R + , ∗ )", complete: false });
  });

  it("at a cut, a run of tokens whose annotation was left out is a formula; plain words are not", () => {
    const tail = quoteSegments("A rule for every widget: gcd ( a , b ) = a", true);
    expect(tail[1]).toMatchObject({ kind: "math", how: "tail", tex: null, tokens: "gcd ( a , b ) = a" });
    expect(quoteSegments("the rank of a", true)).toEqual([{ kind: "text", start: 0, end: 13, text: "the rank of a" }]);
    // Without a cut the same words stay prose.
    expect(quoteSegments("A rule for every widget: gcd ( a , b ) = a").map((s) => s.kind)).toEqual(["text"]);
  });
});

describe("mathHtml", () => {
  it("renders letters, scripts, fractions, radicals, blackboard letters and accents", () => {
    expect(mathHtml("x^{2}+y_{i}=1")).toBe('<span class="atlas-math" title="TeX: x^{2}+y_{i}=1"><i>x</i><sup>2</sup> + <i>y</i><sub><i>i</i></sub> = 1</span>');
    expect(strip(mathHtml("\\frac{1}{2}"))).toBe("12");
    expect(mathHtml("\\frac{1}{2}")).toContain('<span class="m-frac"><span class="m-num">1</span><span class="m-den">2</span></span>');
    expect(mathHtml("\\sqrt{-3}")).toContain('√<span class="m-rad">−3</span>');
    expect(strip(mathHtml("\\mathbb{R}^n \\to \\mathbb{C}"))).toBe("ℝn → ℂ");
    expect(strip(mathHtml("{\\mathcal {P}}(\\mathbb {N})"))).toBe("𝒫(ℕ)");
    // An accent is drawn over its body by the stylesheet (data-mark); the combining mark stays in the text.
    expect(strip(mathHtml("{\\tilde {X}}"))).toBe("X" + String.fromCharCode(0x303));
    expect(mathHtml("{\\tilde {X}}")).toContain('<span class="m-acc is-it" data-mark="˜"><i>X</i><span class="atlas-sr">\u0303</span></span>');
    expect(strip(mathHtml("\\operatorname {gcd}(a,b)"))).toBe("gcd(a, b)");
    expect(strip(mathHtml("\\lim _{x\\to c}f(x)"))).toBe("limx → c" + String.fromCharCode(0x2009) + "f(x)");
    expect(strip(mathHtml("{\\begin{cases}w=5\\\\v=7\\end{cases}}"))).toBe("{w = 5v = 7");
    expect(strip(mathHtml("a\\not\\in B, \\{x\\mid x>0\\}"))).toBe("a ∉ B, {x ∣ x &gt; 0}");
  });

  it("lays matrices, cases and aligned rows out as rows and columns, never joined onto one line", () => {
    const rows = (html: string) => [...html.matchAll(/<span class="m-row">(.*?)<\/span><\/span>(?=<span class="m-row">|<\/span>)/g)].length;
    const pm = mathHtml("{\\begin{pmatrix}w&u\\\\v&t\\end{pmatrix}}");
    expect(pm).toContain('<span class="m-env" data-layout="matrix"><span class="m-delim" data-d="paren" data-side="l"><span class="atlas-sr">(</span></span>' +
      '<span class="m-table"><span class="m-row"><span class="m-cell" data-a="c"><i>w</i></span><span class="m-cell" data-a="c"><i>u</i></span></span>' +
      '<span class="m-row"><span class="m-cell" data-a="c"><i>v</i></span><span class="m-cell" data-a="c"><i>t</i></span></span></span>' +
      '<span class="m-delim" data-d="paren" data-side="r"><span class="atlas-sr">)</span></span></span>');
    expect(rows(pm)).toBe(2);
    expect(strip(pm)).not.toContain(";");
    // cases: the brace scaled to its rows, value and condition flush left
    const cases = mathHtml("g(w)={\\begin{cases}w^{3},&w\\neq 1\\\\2,&w=1\\end{cases}}");
    expect(cases).toContain('<span class="m-delim" data-d="brace" data-side="l"><span class="m-dglyph" style="--m-rows:2">{</span></span>');
    expect(cases).toContain('data-layout="cases"');
    expect(cases.match(/data-a="l"/g)?.length).toBe(4);
    // aligned: right-aligned left-hand sides, the relation starting the tight right-hand cell
    const al = mathHtml("\\begin{aligned}a&=b+c\\\\&=d\\end{aligned}");
    expect(al).toContain('<span class="m-cell" data-a="r"><i>a</i></span><span class="m-cell is-tight" data-a="l">= <i>b</i> + <i>c</i></span>');
    // alignedat's column count is an argument, never a cell; a row's spacing is never shown
    const at = mathHtml("\\begin{alignedat}{13}&(A&&\\land B)\\\\[4pt]&(B&&\\lor A)\\end{alignedat}");
    expect(strip(at)).not.toMatch(/13|4pt|\[/);
    expect(rows(at)).toBe(2);
    for (const tex of ["\\begin{cases}a\\\\[5mu]b\\end{cases}", "x\\\\[-1ex]y", "\\begin{bmatrix}1\\\\[3pt]2\\end{bmatrix}"]) {
      expect(strip(mathHtml(tex)), tex).not.toMatch(/mu\]|ex\]|pt\]/);
    }
    // a bracket after a row break that is not a length stays
    expect(strip(mathHtml("\\begin{matrix}a\\\\[b,c]\\end{matrix}"))).toContain("[b, c]");
  });

  it("stacks a sub- and superscript, and breaks a display formula only after a relation", () => {
    expect(mathHtml("\\sum _{i=0}^{n-1}x_{i}")).toContain('∑<span class="m-ss"><span class="m-sup"><i>n</i> − 1</span><span class="m-sub"><i>i</i> = 0</span></span>');
    const display = mathHtml("f(x)=-g(x)+h(x)=0", true);
    expect(display).toContain('<span class="m-chunk"><i>f</i>(<i>x</i>) =</span> <span class="m-chunk">−<i>g</i>(<i>x</i>) + <i>h</i>(<i>x</i>) =</span> <span class="m-chunk">0</span>');
    expect(mathHtml("f(x)=0")).not.toContain("m-chunk");
    expect(strip(mathHtml("U:=\\{x\\mid x>0\\}"))).toBe("U := {x ∣ x &gt; 0}");
  });

  it("escapes what it shows and what its title carries", () => {
    const html = mathHtml("\\text{<script>} \"x\"");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain('title="TeX: \\text{&lt;script&gt;} &quot;x&quot;"');
  });

  it("shows deeply nested or very long TeX as text instead of recursing into it", () => {
    const deep = "{".repeat(5000) + "x" + "}".repeat(5000);
    expect(() => mathHtml(deep)).not.toThrow();
    expect(strip(mathHtml(deep))).toContain("{{{x}}}");
    expect(() => renderQuote({ exact: `a b {\\displaystyle ${deep}} c` }, false)).not.toThrow();
  });

  it("never throws on broken TeX", () => {
    for (const tex of ["\\frac{", "}}", "^", "\\left(", "\\begin{pmatrix", "\\sqrt[", "{\\mathbb", "\\",
      "\\constructor", "\\toString x", "\\begin{constructor}a\\end{constructor}", "\\__proto__"]) {
      expect(() => mathHtml(tex)).not.toThrow();
    }
  });
});

describe("renderQuote", () => {
  it("shows each formula once and closes the layer's space before punctuation", () => {
    const r = renderQuote({ exact: "the gadget count grows as O ( k 3 ) {\\displaystyle O(k^{3})} ." }, false);
    expect(strip(r.exact)).toBe("the gadget count grows as O(k3).");
    expect(r.exact).not.toContain("displaystyle");
  });

  it("draws a formula straddling the prefix in the quote itself", () => {
    const r = renderQuote({ prefix: "where x = ", exact: "1 {\\displaystyle x=1} holds", suffix: "" }, false);
    expect(strip(r.prefix)).toBe("where ");
    expect(strip(r.exact)).toBe("x = 1 holds");
  });

  it("drops the tail of an annotation a prefix starts inside", () => {
    const r = renderQuote({ prefix: "style X}} and ", exact: "more words here", suffix: "" }, false);
    expect(strip(r.prefix)).toBe(" and ");
  });
});

describe("quoteFragment", () => {
  it("quotes a quote with no formula whole, as before", () => {
    expect(quoteFragment("every finite widget admits a spanning gadget")).toBe("every%20finite%20widget%20admits%20a%20spanning%20gadget");
  });

  it("builds a start and end from the prose around formulas, with hyphens encoded", () => {
    expect(quoteFragment("The weight of a widget f {\\displaystyle f} from the gadget space"))
      .toBe("The%20weight%20of%20a%20widget,from%20the%20gadget%20space");
    expect(quoteFragment("for a balanced p q {\\displaystyle {\\tfrac {p}{q}}} -widget (a p-widget with"))
      .toBe("for%20a%20balanced,%2Dwidget%20(a%20p%2Dwidget%20with");
  });

  it("gives no fragment when no run of prose is specific enough to find", () => {
    expect(quoteFragment("Pick u ∈ R k {\\displaystyle u\\in \\mathbb {R} ^{k}} as a")).toBeNull();
    expect(quoteFragment("w G = g ( w H ) {\\displaystyle w_{G}=g(w_{H})}")).toBeNull();
  });
});

describe("passageTextHtml", () => {
  it("renders a passage's segments: prose verbatim, a citation marker small, each formula once from its TeX", () => {
    const html = passageTextHtml([{ text: "If " }, { tex: "w:{\\tilde {G}}\\to G" }, { text: " is a gadget," }, { ref: "[8]" },
      { text: " then " }, { tex: "\\pi_1(G)", display: true }, { text: " ." }]);
    expect(html).toBe(`If ${mathHtml("w:{\\tilde {G}}\\to G")} is a gadget,<sup class="atlas-ref">[8]</sup> then ` +
      `<span class="atlas-math-display">${mathHtml("\\pi_1(G)")}</span>.`);
    expect(strip(html!)).not.toMatch(/\\[A-Za-z]|displaystyle/);
  });

  it("draws a formula the passage's edge cuts from its layer text, as a quote's cut formula", () => {
    const cut = "w G = g ( w H ) {\\displaystyle w_{G}=g(w";
    expect(passageTextHtml([{ tex: "w_{G}=g(w_{H})", cut }, { text: " holds." }]))
      .toBe(`${renderQuote({ exact: cut }, true).exact} holds.`);
  });

  it("returns null for anything but a list of the four shapes, so the page shows the quote", () => {
    for (const bad of [null, "x", [], [{ text: 1 }], [{ foo: "x" }], [null], [{ text: "a" }, 5]]) {
      expect(passageTextHtml(bad), JSON.stringify(bad)).toBeNull();
    }
    expect(passageTextHtml([{ text: "<b>&</b>" }])).toBe("&lt;b&gt;&amp;&lt;/b&gt;");
  });
});
