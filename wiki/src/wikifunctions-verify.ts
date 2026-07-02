// Static explainer for HOW WikiLean formally verifies Wikifunctions against
// Mathlib — the methodology and the actual proofs. Served at
// GET /wikifunctions/verify (index.ts) and KV-cached. Sibling of
// wikifunctions.ts: same inline <style> palette/fonts, same wl-header/wl-nav/
// footer, same local `esc` twin, no D1. Self-contained — the only "data" is the
// verbatim Python/Lean snippets and case counts, which are accurate to the code
// in the standalone Deicyde/wikifunctions repo (Z13701.lean, Z13667.lean,
// native/leanpy/Main.lean, native/difftest.py).

// Local twin of engine/html.ts htmlEscape — keeps this module import-free
// (matches wikifunctions.ts / home.ts).
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

const GH_BLOB = "https://github.com/Deicyde/wikifunctions/blob/main/";

// A GitHub source-file link rendered as a monospace path.
function srcLink(path: string, label?: string): string {
  const href = `${GH_BLOB}${path}`;
  return `<a class="src" href="${esc(href)}" target="_blank" rel="noopener"><code>${esc(label ?? path)}</code></a>`;
}

export function wikifunctionsVerifyPage(): string {
  // Verbatim deployed Python (shown in code blocks). Kept as plain strings and
  // escaped so the `<`/`>`/`&` of any future snippet can't break out.
  const pyCoprime = [
    "def Z13701(a, b):",
    "    while b != 0:",
    "        a, b = b, a % b",
    "    return a == 1",
  ].join("\n");
  const pyFactorial = [
    "def Z13667(n):",
    "    k = 1",
    "    for i in range(1, n + 1):",
    "        k *= i",
    "    return k",
  ].join("\n");

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How we verify Wikifunctions — WikiLean</title>
<meta name="description" content="The verification methodology behind WikiLean's Wikifunctions work: three layers of assurance — a kernel-checked Lean proof that the deployed Python meets its Mathlib spec, a cross-process differential test against real CPython, and lean.py running the genuine interpreter in-process. Two functions verified end-to-end.">
<link rel="canonical" href="https://wikilean.jackmccarthy.org/wikifunctions/verify">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:type" content="article">
<meta property="og:site_name" content="WikiLean">
<meta property="og:title" content="How we verify Wikifunctions — WikiLean">
<meta property="og:description" content="Three layers of assurance: a kernel-checked Lean proof (∀ inputs), a differential test vs real CPython, and lean.py running the genuine interpreter — the deployed Python proved equal to its Mathlib spec.">
<meta property="og:url" content="https://wikilean.jackmccarthy.org/wikifunctions/verify">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="How we verify Wikifunctions — WikiLean">
<meta name="twitter:description" content="The deployed Python, proved equal to its Mathlib spec for all inputs — and cross-checked against real CPython.">
<script>
/* Set the theme attr BEFORE the stylesheet parses so dark mode applies on first
   paint (no flash). Priority: localStorage > OS preference > light default. The
   toggle in the header (script at the bottom of body) flips dark/light. */
(function(){try{var s=localStorage.getItem("wl-theme");
var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
document.documentElement.dataset.theme=t;}catch(e){}})();
</script>
<style>
:root {
  --paper:#f7f4ee; --surface:#fffdf9; --ink:#1f1d1a; --muted:#5f594e;
  --line:#e6e0d2; --line-strong:#d8d0bd;
  --accent:#1a4b8c; --accent-dark:#163e74;
  --green:#2f7d4f; --yellow:#b08020; --red:#b3372f;
  --serif:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
/* Dark mode — remap the warm-light token palette to the shared dark scheme
   (matches pages.ts / style.css). One override block recolors everything that
   uses the vars; the few hardcoded colors below get explicit dark overrides. */
[data-theme="dark"] :root {
  --paper:#1a1816; --surface:#232020; --ink:#ebe5d8; --muted:#9a9081;
  --line:#3a3530; --line-strong:#4d4742; --accent:#6e9adf; --accent-dark:#8fb4e8;
  --green:#8fd4ad; --yellow:#e2bf78; --red:#f08e85;
}
/* Hardcoded-color overrides for dark mode (inline code chips, the Lean-line and
   results/bounds inline-code backgrounds, the ZID badge) — kept legible on the
   dark surface. */
[data-theme="dark"] .lede code, [data-theme="dark"] .prose code,
[data-theme="dark"] .lean-line, [data-theme="dark"] .results code,
[data-theme="dark"] .bounds code { background:#2e2a2f; }
[data-theme="dark"] .zid-badge { color:#6e9adf; background:rgba(110,154,223,.16); border-color:#33486a; }
/* Theme-toggle button (matches the article-page version in style.css). */
.wl-theme-toggle { background:transparent; border:1px solid var(--line-strong); color:var(--muted);
  border-radius:50%; width:28px; height:28px; padding:0; line-height:1; font-size:14px; cursor:pointer;
  display:inline-flex; align-items:center; justify-content:center; }
.wl-theme-toggle:hover { color:var(--ink); border-color:var(--accent); }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--paper); color:var(--ink); line-height:1.55;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.sr { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.wl-header { display:flex; align-items:baseline; justify-content:space-between;
  gap:8px 20px; flex-wrap:wrap; max-width:880px; margin:0 auto; padding:22px 20px 0; }
.wl-brand { font-family:var(--serif); font-weight:700; font-size:1.15rem;
  color:var(--ink); text-decoration:none; }
.wl-brand:hover { color:var(--accent); }
.wl-nav { display:flex; gap:6px 18px; flex-wrap:wrap; }
.wl-nav a { color:var(--accent); text-decoration:none; font-size:.88rem; }
.wl-nav a:hover { text-decoration:underline; }
.wrap { max-width:880px; margin:0 auto; padding:0 20px 56px; }
.hero { padding:42px 0 6px; }
h1 { font-family:var(--serif); font-size:2.4rem; line-height:1.12; letter-spacing:-.01em; margin:0 0 14px; }
.tagline { font-family:var(--serif); font-size:1.24rem; line-height:1.45; margin:0 0 14px; max-width:34em; }
.lede { color:var(--muted); font-size:.98rem; max-width:44em; margin:0 0 10px; }
.lede a, .prose a { color:var(--accent); text-decoration:none; }
.lede a:hover, .prose a:hover { text-decoration:underline; }
.lede code, .prose code { font-family:var(--mono); font-size:.86em; background:#efe9dc;
  padding:1px 5px; border-radius:4px; }
.backlink { margin:4px 0 0; font-size:.9rem; }
.backlink a { color:var(--accent); text-decoration:none; }
.backlink a:hover { text-decoration:underline; }
h2 { font-family:var(--serif); font-size:1.5rem; margin:46px 0 12px; }
h3 { font-family:var(--serif); font-size:1.18rem; margin:30px 0 8px; }
.prose { font-size:.96rem; max-width:46em; margin:0 0 12px; }
.prose.muted { color:var(--muted); }
.prose b { color:var(--ink); font-weight:600; }
.sect-intro { color:var(--muted); font-size:.92rem; max-width:46em; margin:0 0 18px; }
.green { color:var(--green); font-weight:600; }
.green-note { display:flex; align-items:baseline; gap:9px; margin:26px 0 0; padding:13px 16px;
  background:var(--surface); border:1px solid var(--line-strong); border-left:3px solid var(--green);
  border-radius:7px; font-size:.92rem; color:var(--ink); }
.green-note .dot { color:var(--green); font-weight:700; flex:none; }
.green-note code { font-family:var(--mono); font-size:.85em; }
/* layers comparison table */
.table-scroll { overflow-x:auto; border:1px solid var(--line-strong); border-radius:9px;
  background:var(--surface); margin:8px 0 0; }
table.layers { width:100%; border-collapse:collapse; font-size:.9rem; }
.layers thead th { text-align:left; font-weight:600; font-size:.7rem; letter-spacing:.06em;
  text-transform:uppercase; color:var(--muted); padding:11px 14px; border-bottom:1px solid var(--line-strong);
  white-space:nowrap; }
.layers tbody td, .layers tbody th { padding:12px 14px; border-bottom:1px solid var(--line);
  vertical-align:top; text-align:left; font-weight:400; }
.layers tbody tr:last-child td, .layers tbody tr:last-child th { border-bottom:0; }
.layers tbody th { font-weight:600; white-space:nowrap; }
.layers .ly-name { font-family:var(--serif); font-size:1rem; }
.layers .ly-tool { display:block; font-family:var(--mono); font-size:.78rem; color:var(--muted);
  margin-top:2px; font-weight:400; }
.layers code { font-family:var(--mono); font-size:.84em; }
.layers .cov { white-space:nowrap; }
.trust-none { color:var(--green); font-weight:600; }
/* code blocks — the site code styling, scaled up for a block */
pre.code { font-family:var(--mono); font-size:.84rem; line-height:1.5; margin:14px 0;
  padding:14px 16px; background:var(--surface); border:1px solid var(--line-strong);
  border-radius:9px; overflow-x:auto; color:var(--ink); }
pre.code .cmt { color:var(--muted); }
.lean-line { font-family:var(--mono); font-size:.84rem; background:#efe9dc; padding:2px 6px;
  border-radius:4px; display:inline-block; color:var(--ink); }
/* worked-example card */
.example { margin:22px 0 0; padding:18px 20px; background:var(--surface);
  border:1px solid var(--line-strong); border-radius:11px; }
.example h3 { margin:0 0 4px; }
.zid-badge { font-family:var(--mono); font-size:.78rem; font-weight:600; color:var(--accent);
  background:#eef3fa; border:1px solid #bcd0ea; border-radius:5px; padding:2px 8px;
  vertical-align:middle; margin-left:6px; }
.example .step { margin:14px 0 0; }
.example .step .lbl { font-size:.72rem; letter-spacing:.07em; text-transform:uppercase;
  color:var(--muted); margin:0 0 4px; }
.example .step p { margin:0; font-size:.93rem; }
.results { list-style:none; margin:14px 0 0; padding:0; font-size:.93rem; }
.results li { padding:5px 0 5px 22px; position:relative; }
.results li::before { content:"\\2713"; position:absolute; left:0; color:var(--green); font-weight:700; }
.results code { font-family:var(--mono); font-size:.85em; background:#efe9dc; padding:1px 5px; border-radius:4px; }
.results .num { color:var(--green); font-weight:600; font-variant-numeric:tabular-nums; }
/* honest-boundaries list */
.bounds { margin:8px 0 0; padding:0 0 0 20px; font-size:.95rem; }
.bounds li { margin:9px 0; max-width:46em; }
.bounds li b { color:var(--ink); font-weight:600; }
.bounds code { font-family:var(--mono); font-size:.86em; background:#efe9dc; padding:1px 5px; border-radius:4px; }
/* source / reproduce */
.src-grid { display:grid; gap:9px; margin:8px 0 0; }
.src-grid .li { background:var(--surface); border:1px solid var(--line); border-radius:7px;
  padding:10px 13px; font-size:.9rem; }
.src-grid .li .src code { font-family:var(--mono); font-size:.82rem; color:var(--accent); background:none; padding:0; }
.src-grid .li a.src { text-decoration:none; }
.src-grid .li a.src:hover code { text-decoration:underline; }
.src-grid .li .desc { color:var(--muted); }
footer { margin-top:56px; padding-top:18px; border-top:1px solid var(--line-strong);
  color:var(--muted); font-size:.82rem; line-height:1.6; }
footer p { margin:4px 0; }
footer a { color:var(--accent); text-decoration:none; }
footer a:hover { text-decoration:underline; }
@media (max-width:540px) {
  h1 { font-size:1.95rem; }
  .tagline { font-size:1.08rem; }
  .hero { padding-top:30px; }
  h2 { font-size:1.3rem; }
}
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav" aria-label="Site">
    <a href="/concepts">Concepts</a>
    <a href="/wikifunctions">Wikifunctions</a>
    <a href="/wikifunctions/verify">How we verify</a>
    <a href="/article-graph">Article graph</a>
    <a href="/graph">Concept graph</a>
    <a href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<main class="wrap">
  <section class="hero">
    <h1>How we verify Wikifunctions</h1>
    <p class="tagline">The deployed code, <em>proved</em> equal to its Mathlib
      specification — and cross-checked against real CPython.</p>
    <p class="backlink">&larr; <a href="/wikifunctions">Back to the spec tracker</a></p>
    <p class="lede">
      We give <a href="https://www.wikifunctions.org">Wikifunctions</a>&#x27; executable functions the
      formal specification they lack, by joining each function to a
      <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib</a> (Lean&nbsp;4)
      declaration via its shared Wikidata QID, then <em>proving</em> the deployed code meets that spec.
      Two functions are fully verified end-to-end so far; the rest of the addressable set has specs
      (see the <a href="/wikifunctions">spec tracker</a>).
    </p>
  </section>

  <h2>Three layers of assurance</h2>
  <p class="sect-intro">Each layer compares something different, covers a different slice of inputs, and
    rests on a different trust assumption. They are complementary: the proof is the strongest claim but
    rests on a model of Python; the lean.py check executes the genuine interpreter and closes exactly
    that gap.</p>

  <div class="table-scroll">
    <table class="layers">
      <thead>
        <tr>
          <th scope="col">Layer</th>
          <th scope="col">What it compares</th>
          <th scope="col">Coverage</th>
          <th scope="col">Trust assumption</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th scope="row"><span class="ly-name">Deductive proof</span><span class="ly-tool">Lean</span></th>
          <td>the deployed Python, embedded in Lean, &hArr; the Mathlib spec</td>
          <td class="cov"><b>all inputs</b> (&forall;), kernel-checked</td>
          <td>our Lean model of Python&#x27;s semantics matches CPython</td>
        </tr>
        <tr>
          <th scope="row"><span class="ly-name">Differential test</span><span class="ly-tool">cross-process</span></th>
          <td>real CPython vs our compiled embedding</td>
          <td class="cov">829 cases (coprime)</td>
          <td><span class="trust-none">none</span> &mdash; runs real Python</td>
        </tr>
        <tr>
          <th scope="row"><span class="ly-name">lean.py in-process</span><span class="ly-tool">embedded CPython</span></th>
          <td>real CPython (loaded into Lean) vs the Mathlib spec</td>
          <td class="cov">1607 (coprime) / 21 (factorial)</td>
          <td><span class="trust-none">none</span> &mdash; runs the genuine interpreter</td>
        </tr>
      </tbody>
    </table>
  </div>

  <p class="prose" style="margin-top:18px;">The <b>deductive proof</b> is the strongest result: it is a
    theorem about <em>every</em> input, checked by Lean&#x27;s kernel. But a proof about embedded Python is
    only as good as the embedding &mdash; it rests on the one assumption that our Lean model of Python&#x27;s
    semantics faithfully matches what CPython actually does. The <b>lean.py</b> layer closes precisely that
    gap: it loads the real CPython interpreter into the Lean process and runs the genuine deployed source,
    so its agreement with the spec carries <span class="green">no semantics assumption at all</span>. The
    cross-process <b>differential test</b> is a third, independent witness from outside the Lean process.
    Together: the proof gives universal coverage, and the two CPython checks empirically discharge the
    proof&#x27;s lone assumption.</p>

  <h2>Worked example 1 &mdash; &ldquo;are coprime&rdquo; <span class="zid-badge">Z13701</span></h2>
  <div class="example">
    <h3>The deployed Python</h3>
    <pre class="code">${esc(pyCoprime)}</pre>
    <div class="step">
      <p class="lbl">The Lean embedding</p>
      <p>The program is encoded as data over a tiny imperative-Python AST
        (${srcLink("Wikifunctions/Python/Imp.lean", "Imp.lean")}) with an operational
        semantics; then we prove it equal to Mathlib&#x27;s <code>Nat.Coprime</code>:</p>
      <p style="margin-top:10px;"><span class="lean-line">theorem runProgram_eq_coprime (a b : Nat) : runProgram a b = some (decide (Nat.Coprime a b))</span></p>
    </div>
    <ul class="results">
      <li>Proved for <b>all</b> <code>a, b</code> with clean axioms <code>[propext, Classical.choice, Quot.sound]</code> &mdash; no <code>sorry</code>, no <code>native_decide</code>.</li>
      <li>Differential test: <span class="num">829&#8202;/&#8202;829</span>, <span class="num">0 mismatches</span>.</li>
      <li>lean.py in-process: <span class="num">1607&#8202;/&#8202;1607</span>.</li>
    </ul>
  </div>

  <h2>Worked example 2 &mdash; &ldquo;factorial&rdquo; <span class="zid-badge">Z13667</span></h2>
  <div class="example">
    <h3>The deployed Python</h3>
    <pre class="code">${esc(pyFactorial)}</pre>
    <div class="step">
      <p class="lbl">The theorem</p>
      <p><span class="lean-line">theorem runFac_eq_factorial (n : Nat) : runFac n = some (Nat.factorial n)</span></p>
    </div>
    <ul class="results">
      <li>Proved (clean axioms <code>[propext, Classical.choice, Quot.sound]</code>).</li>
      <li>lean.py in-process: <span class="num">21&#8202;/&#8202;21</span> vs <code>Nat.factorial</code>.</li>
    </ul>
  </div>

  <h2>Honest boundaries</h2>
  <p class="sect-intro">These are features of an honest verification claim, not disclaimers to bury.</p>
  <ul class="bounds">
    <li>Only <b>2 of 25</b> addressable functions are <em>proved</em> so far; the others have specs / oracles
      (tracked on the <a href="/wikifunctions">spec page</a>).</li>
    <li>We also have <b>Dafny + Verus</b> proofs of coprime &mdash; but those verify a <em>re-implementation</em>
      against a spec written in the prover&#x27;s own logic. That is a weaker, different claim than the Lean one
      (&ldquo;the actual Python &hArr; Mathlib&rdquo;).</li>
    <li>The deductive proof&#x27;s one assumption (Lean semantics &asymp; CPython) is the irreducible gap of
      verifying any real code. lean.py mitigates it empirically by running the genuine interpreter.</li>
  </ul>

  <h2>Source &amp; reproduce</h2>
  <p class="sect-intro">Every claim above is in the repository &mdash; the embedding, the two proofs, and both
    CPython cross-checks.</p>
  <div class="src-grid">
    <div class="li">${srcLink("Wikifunctions/Python/Imp.lean")}
      <span class="desc"> &mdash; the imperative-Python embedding (AST + operational semantics).</span></div>
    <div class="li">${srcLink("Wikifunctions/Python/Z13701.lean")}
      <span class="desc"> &mdash; the coprime proof (<code>runProgram_eq_coprime</code>).</span></div>
    <div class="li">${srcLink("Wikifunctions/Python/Z13667.lean")}
      <span class="desc"> &mdash; the factorial proof (<code>runFac_eq_factorial</code>).</span></div>
    <div class="li">${srcLink("native/leanpy/Main.lean")}
      <span class="desc"> &mdash; lean.py: real CPython loaded in-process, checked against the spec.</span></div>
    <div class="li">${srcLink("native/difftest.py")}
      <span class="desc"> &mdash; the cross-process differential test vs real CPython.</span></div>
  </div>
  <p class="prose muted" style="margin-top:16px;">Reproduce the proofs:</p>
  <pre class="code">git clone https://github.com/Deicyde/wikifunctions &amp;&amp; cd wikifunctions
lake exe cache get
lake build Wikifunctions.Python.Z13701 Wikifunctions.Python.Z13667</pre>

  <div class="green-note">
    <span class="dot">&#10003;</span>
    <span>Both proofs build green with <b>no <code>sorry</code></b> and clean axioms; the two CPython
      cross-checks report <b>zero mismatches</b>.</span>
  </div>

  <footer>
    <p>Wikifunctions data &copy; its contributors, available under
      <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>;
      Mathlib is <a href="https://github.com/leanprover-community/mathlib4">Apache-2.0</a>.
      WikiLean&#x27;s mappings and proofs are released under
      <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>.</p>
    <p><a href="https://github.com/Deicyde/WikiLean">Source on GitHub</a> &middot;
      a project by <a href="https://jackmccarthy.org">Jack McCarthy</a></p>
  </footer>
</main>
<script>
/* Theme toggle — flips dark/light and persists in localStorage. */
(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;
b.addEventListener("click",function(){var r=document.documentElement;
var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;
try{localStorage.setItem("wl-theme",n);}catch(e){}});})();
</script>
</body>
</html>
`;
}
