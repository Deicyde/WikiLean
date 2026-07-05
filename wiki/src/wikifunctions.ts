// Static status/tracker page for the "Wikifunctions formalization" project.
// Served at GET /wikifunctions (index.ts) and KV-cached. The data is the small
// verified corpus embedded in wikifunctions-data.ts — no D1, no migrations; the
// page is fully self-contained.
//
// Visual language mirrors home.ts exactly: warm academic-minimalist paper
// background, serif display headings (system stacks only), one deep-blue
// accent, the three semantic status colors. Header/footer match the site so the
// page belongs rather than being bolted on. Import-free apart from the data
// constant, with a local `esc` twin (same as home.ts).

import { WF_FUNCTIONS, type WfFunction, type Tier, type Faithfulness, type Computable } from "./wikifunctions-data.js";

// Local twin of engine/html.ts htmlEscape — keeps this module's only import the
// data constant (matches home.ts).
function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

const MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs/";

const TIER_META: Record<Tier, { label: string; blurb: string }> = {
  composite_provable: {
    label: "composite-provable",
    blurb:
      "computable/decidable ℕ or ℤ oracle — conformance is provable in-kernel with <code>decide</code>",
  },
  oracle_testable: {
    label: "oracle-testable",
    blurb:
      "computable oracle as differential-test ground truth: exact ℚ/ℤ arithmetic, plus <code>Float</code> (IEEE-754 binary64) oracles for float64 functions",
  },
  spec_only: {
    label: "spec-only",
    blurb: "noncomputable real oracle only (e.g. arbitrary-precision Gamma) — specifies exact-ℝ behavior",
  },
};

const TIER_ORDER: Tier[] = ["composite_provable", "oracle_testable", "spec_only"];

const FAITH_META: Record<Faithfulness, { label: string; cls: string }> = {
  faithful: { label: "faithful", cls: "ok" },
  wrong_target: { label: "wrong target", cls: "warn" },
  representation_mismatch: { label: "repr. mismatch", cls: "info" },
};

const COMPUTABLE_LABEL: Record<Computable, string> = {
  decidable: "decidable",
  computable_nat_int: "computable (ℕ/ℤ)",
  float: "float64",
  noncomputable_real: "noncomputable ℝ",
};

// Mathlib docs deep-link for a bare decl name. The docs site supports a
// `?q=`-style search; the per-decl find page is the closest stable target.
function declCell(fn: WfFunction): string {
  const code = `<code>${esc(fn.oracleDecl)}</code>`;
  if (fn.docDecl === "") return `<span class="decl">${code}</span>`;
  const href = `${MATHLIB_DOCS}find/?pattern=${encodeURIComponent(fn.docDecl)}#doc`;
  return `<a class="decl" href="${esc(href)}" target="_blank" rel="noopener">${code}</a>`;
}

function row(fn: WfFunction): string {
  const f = FAITH_META[fn.faithfulness];
  const zHref = `https://www.wikifunctions.org/wiki/${esc(fn.zid)}`;
  const qHref = `https://www.wikidata.org/wiki/${esc(fn.qid)}`;
  return (
    `<tr>` +
    `<th scope="row" class="zid">` +
    `<a href="${zHref}" target="_blank" rel="noopener">${esc(fn.zid)}</a>` +
    `<span class="fn-name">${esc(fn.name)}</span></th>` +
    `<td class="qid"><a href="${qHref}" target="_blank" rel="noopener">${esc(fn.qid)}</a></td>` +
    `<td class="oracle">${declCell(fn)}</td>` +
    `<td class="comp">${esc(COMPUTABLE_LABEL[fn.computable])}</td>` +
    `<td class="faith"><span class="chip ${f.cls}" title="${esc(fn.faithfulnessNote)}">${f.label}</span></td>` +
    `</tr>`
  );
}

function tierSection(tier: Tier): string {
  const fns = WF_FUNCTIONS.filter((f) => f.tier === tier);
  const meta = TIER_META[tier];
  const rows = fns.map(row).join("\n");
  return `
  <section class="tier" id="tier-${tier}" aria-labelledby="${tier}-h">
    <div class="tier-head">
      <h3 id="${tier}-h"><code class="tier-tag tt-${tier}">${meta.label}</code>
        <span class="tier-count">${fns.length}</span></h3>
      <p class="tier-blurb">${meta.blurb}</p>
    </div>
    <div class="table-scroll">
      <table class="fn-table">
        <thead>
          <tr>
            <th scope="col">Function</th>
            <th scope="col">Wikidata</th>
            <th scope="col">Mathlib oracle</th>
            <th scope="col">Computability</th>
            <th scope="col">Faithfulness</th>
          </tr>
        </thead>
        <tbody>
${rows}
        </tbody>
      </table>
    </div>
  </section>`;
}

export function wikifunctionsPage(): string {
  const total = WF_FUNCTIONS.length;
  const nComposite = WF_FUNCTIONS.filter((f) => f.tier === "composite_provable").length;
  const nTestable = WF_FUNCTIONS.filter((f) => f.tier === "oracle_testable").length;
  const nSpecOnly = WF_FUNCTIONS.filter((f) => f.tier === "spec_only").length;
  const nFaithful = WF_FUNCTIONS.filter((f) => f.faithfulness === "faithful").length;
  const sections = TIER_ORDER.map(tierSection).join("\n");

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wikifunctions formalization — WikiLean</title>
<meta name="description" content="Tracking WikiLean's effort to give Wikifunctions executable functions a formal specification from Mathlib: 25 addressable functions, each pinned to a computable Lean oracle and graded for faithfulness, building green against Mathlib.">
<link rel="canonical" href="https://wikilean.jackmccarthy.org/wikifunctions">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="WikiLean">
<meta property="og:title" content="Wikifunctions formalization — WikiLean">
<meta property="og:description" content="25 Wikifunctions pinned to a computable Mathlib oracle and a verified Lean spec — the formal specification an executable function library lacks.">
<meta property="og:url" content="https://wikilean.jackmccarthy.org/wikifunctions">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Wikifunctions formalization — WikiLean">
<meta name="twitter:description" content="25 Wikifunctions pinned to a computable Mathlib oracle and a verified Lean spec.">
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
/* Hardcoded-color overrides for dark mode (inline code, tier tags, chips,
   row-hover tints) so they stay legible on the dark surface. */
[data-theme="dark"] .lede code { background:#2e2a2f; }
[data-theme="dark"] .tt-composite_provable { color:#8fd4ad; border-color:#2f5b44; background:rgba(76,169,122,.16); }
[data-theme="dark"] .tt-oracle_testable { color:#6e9adf; border-color:#33486a; background:rgba(110,154,223,.16); }
[data-theme="dark"] .tt-spec_only { color:#e2bf78; border-color:#5c4f31; background:rgba(212,160,66,.16); }
[data-theme="dark"] .fn-table tbody tr:hover { background:rgba(110,154,223,.08); }
[data-theme="dark"] .chip.ok { color:#8fd4ad; background:rgba(76,169,122,.16); border-color:#2f5b44; }
[data-theme="dark"] .chip.warn { color:#f08e85; background:rgba(226,104,95,.16); border-color:#6a3a36; }
[data-theme="dark"] .chip.info { color:#e2bf78; background:rgba(212,160,66,.16); border-color:#5c4f31; }
[data-theme="dark"] .crosscheck code { background:#2e2a2f; }
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
.lede a { color:var(--accent); text-decoration:none; }
.lede a:hover { text-decoration:underline; }
.lede code { font-family:var(--mono); font-size:.86em; background:#efe9dc;
  padding:1px 5px; border-radius:4px; }
.verify-pointer { font-size:.92rem; margin:0 0 18px; color:var(--muted); }
.verify-pointer a { color:var(--accent); text-decoration:none; font-weight:600; }
.verify-pointer a:hover { text-decoration:underline; }
.stats { display:flex; gap:16px 40px; flex-wrap:wrap; margin:30px 0 6px; }
.stat-num { display:block; font-family:var(--serif); font-weight:700; font-size:1.8rem;
  line-height:1.15; font-variant-numeric:tabular-nums; }
.stat-label { display:block; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--muted); margin-top:2px; }
.green-note { display:flex; align-items:baseline; gap:9px; margin:26px 0 0; padding:13px 16px;
  background:var(--surface); border:1px solid var(--line-strong); border-left:3px solid var(--green);
  border-radius:7px; font-size:.92rem; color:var(--ink); }
.green-note .dot { color:var(--green); font-weight:700; flex:none; }
.green-note code { font-family:var(--mono); font-size:.85em; }
h2 { font-family:var(--serif); font-size:1.35rem; margin:42px 0 12px; }
.sect-intro { color:var(--muted); font-size:.92rem; max-width:44em; margin:0 0 18px; }
.legend-grid { display:grid; gap:10px; margin:0 0 8px; }
.legend-grid .li { background:var(--surface); border:1px solid var(--line); border-radius:7px;
  padding:10px 13px; font-size:.88rem; }
.legend-grid .li code { font-family:var(--mono); font-size:.84em; }
.legend-grid .li .nm { font-weight:600; }
.legend-grid .li .desc { color:var(--muted); }
.tier { margin:30px 0 0; }
.tier-head { margin:0 0 12px; }
.tier-head h3 { font-family:var(--serif); font-size:1.12rem; margin:0; display:flex;
  align-items:center; gap:10px; }
.tier-tag { font-family:var(--mono); font-size:.78rem; font-weight:600; padding:2px 8px;
  border-radius:5px; border:1px solid var(--line-strong); }
.tt-composite_provable { color:var(--green); border-color:#bcd9c6; background:#eef6f0; }
.tt-oracle_testable { color:var(--accent); border-color:#bcd0ea; background:#eef3fa; }
.tt-spec_only { color:var(--yellow); border-color:#e3d3ac; background:#f7f0df; }
.tier-count { font-family:var(--serif); font-weight:700; font-size:1.05rem; color:var(--muted);
  font-variant-numeric:tabular-nums; }
.tier-blurb { color:var(--muted); font-size:.85rem; margin:6px 0 0; max-width:46em; }
.tier-blurb code { font-family:var(--mono); font-size:.86em; }
.table-scroll { overflow-x:auto; border:1px solid var(--line-strong); border-radius:9px;
  background:var(--surface); }
table.fn-table { width:100%; border-collapse:collapse; font-size:.88rem; }
.fn-table thead th { text-align:left; font-weight:600; font-size:.7rem; letter-spacing:.06em;
  text-transform:uppercase; color:var(--muted); padding:11px 14px; border-bottom:1px solid var(--line-strong);
  white-space:nowrap; }
.fn-table tbody td, .fn-table tbody th { padding:10px 14px; border-bottom:1px solid var(--line);
  vertical-align:top; text-align:left; font-weight:400; }
.fn-table tbody tr:last-child td, .fn-table tbody tr:last-child th { border-bottom:0; }
.fn-table tbody tr:hover { background:rgba(26,75,140,.04); }
.zid { white-space:nowrap; }
.zid a { font-family:var(--mono); font-size:.86rem; color:var(--accent); text-decoration:none; font-weight:600; }
.zid a:hover { text-decoration:underline; }
.fn-name { display:block; color:var(--ink); font-size:.85rem; margin-top:2px; }
.qid a { font-family:var(--mono); font-size:.84rem; color:var(--accent); text-decoration:none; }
.qid a:hover { text-decoration:underline; }
.oracle { min-width:200px; }
.oracle .decl { text-decoration:none; }
.oracle a.decl:hover code { color:var(--accent); text-decoration:underline; }
.oracle code { font-family:var(--mono); font-size:.82rem; color:var(--ink);
  white-space:normal; word-break:break-word; }
.comp { color:var(--muted); white-space:nowrap; font-size:.84rem; }
.chip { display:inline-block; font-size:.76rem; font-weight:600; padding:2px 9px; border-radius:11px;
  white-space:nowrap; cursor:help; border:1px solid transparent; }
.chip.ok { color:var(--green); background:#eef6f0; border-color:#bcd9c6; }
.chip.warn { color:var(--red); background:#f8ecea; border-color:#e6c2bd; }
.chip.info { color:var(--yellow); background:#f7f0df; border-color:#e3d3ac; }
.crosscheck { margin:34px 0 0; padding:16px 18px; background:var(--surface);
  border:1px solid var(--line-strong); border-radius:9px; }
.crosscheck h2 { margin:0 0 8px; font-size:1.12rem; }
.crosscheck p { margin:0; color:var(--muted); font-size:.92rem; }
.crosscheck p + p { margin-top:8px; }
.crosscheck code { font-family:var(--mono); font-size:.86em; background:#efe9dc;
  padding:1px 5px; border-radius:4px; }
.next { margin:26px 0 0; color:var(--muted); font-size:.92rem; }
.next b { color:var(--ink); font-weight:600; }
footer { margin-top:56px; padding-top:18px; border-top:1px solid var(--line-strong);
  color:var(--muted); font-size:.82rem; line-height:1.6; }
footer p { margin:4px 0; }
footer a { color:var(--accent); text-decoration:none; }
footer a:hover { text-decoration:underline; }
@media (max-width:540px) {
  h1 { font-size:1.95rem; }
  .tagline { font-size:1.08rem; }
  .stat-num { font-size:1.5rem; }
  .stats { gap:14px 26px; margin-top:24px; }
  .hero { padding-top:30px; }
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
    <a href="/brain">Brain</a>
    <a href="/about">About &amp; method</a>
    <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<main class="wrap">
  <section class="hero">
    <h1>Wikifunctions formalization</h1>
    <p class="tagline">Giving <a href="https://www.wikifunctions.org">Wikifunctions</a>&#x27; executable
      functions the formal specification they lack — borrowed from Mathlib.</p>
    <p class="verify-pointer">&rarr; <a href="/wikifunctions/verify">How these are verified</a> &mdash;
      the deployed code, proved equal to its Mathlib spec.</p>
    <p class="lede">
      WikiLean joins each Wikidata mathematics concept to two things: an <a
      href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib</a> formalization, and a
      Wikifunctions executable function (via the <code>wikifunctionswiki</code> sitelink on the shared
      Wikidata item). The Wikidata QID is the join key. So for a function Wikifunctions can <em>run</em>
      but cannot <em>prove anything about</em>, the matching Mathlib declaration becomes the formal
      spec it was missing.
    </p>
    <p class="lede">
      Of the <b>1,904</b> Wikifunctions currently linked to Wikidata, <b>${total}</b> fall in WikiLean&#x27;s
      addressable set. For each we pin a <em>computable oracle</em> (the Mathlib decl or composed
      expression that produces the ground-truth value), grade whether that oracle is actually
      <em>faithful</em> to what the Wikifunction computes, and write a Lean spec stating its defining
      properties.
    </p>
    <div class="stats">
      <div class="stat"><span class="stat-num">1,904</span><span class="stat-label">WF &harr; Wikidata</span></div>
      <div class="stat"><span class="stat-num">${total}</span><span class="stat-label">addressable</span></div>
      <div class="stat"><span class="stat-num">${nFaithful}/${total}</span><span class="stat-label">faithful</span></div>
      <div class="stat"><span class="stat-num">${nComposite}</span><span class="stat-label">composite-provable</span></div>
      <div class="stat"><span class="stat-num">${nTestable}</span><span class="stat-label">oracle-testable</span></div>
      <div class="stat"><span class="stat-num">${nSpecOnly}</span><span class="stat-label">spec-only</span></div>
    </div>
    <div class="green-note">
      <span class="dot">&#10003;</span>
      <span>The Lean spec corpus (all ${total} blocks) <b>builds green against Mathlib</b> &mdash; zero
        <code>sorry</code>, no axiom cheats. Every verdict is independently confirmed.</span>
    </div>
  </section>

  <h2>The 25 addressable functions</h2>
  <p class="sect-intro">Grouped by verification tier. Each row links the Wikifunctions function (ZID),
    its Wikidata item (QID), and the Mathlib oracle pinned as ground truth. Hover a faithfulness chip
    for the reviewer&#x27;s note.</p>

  <div class="legend-grid">
    <div class="li"><span class="nm"><code class="tier-tag tt-composite_provable">composite-provable</code></span>
      <span class="desc"> &mdash; ${TIER_META.composite_provable.blurb}.</span></div>
    <div class="li"><span class="nm"><code class="tier-tag tt-oracle_testable">oracle-testable</code></span>
      <span class="desc"> &mdash; ${TIER_META.oracle_testable.blurb}.</span></div>
    <div class="li"><span class="nm"><code class="tier-tag tt-spec_only">spec-only</code></span>
      <span class="desc"> &mdash; ${TIER_META.spec_only.blurb}.</span></div>
  </div>
${sections}

  <section class="crosscheck">
    <h2>What the cross-check found</h2>
    <p>Pinning a Mathlib oracle doubles as an audit of WikiLean&#x27;s own AI-generated tags. As
      originally mapped, only <b>9 of 25</b> were faithful for spec purposes: a recurring failure was a
      tag pointing at a <em>typeclass</em> (the abstract algebraic structure, e.g. <code>GCDMonoid</code>,
      <code>AddCommMonoid</code>) rather than the computable <em>operation</em> (<code>Nat.gcd</code>,
      <code>Nat.add</code>). A typeclass can never serve as a value oracle. Each such case was corrected
      to a concrete computable oracle, lifting the corpus to <b>${nFaithful}/${total}</b> faithful.</p>
    <p>The remaining non-faithful rows are honest representation mismatches (right concept, wrong
      encoding &mdash; e.g. a Gaussian-integer pair vs Mathlib&#x27;s <code>&#8450;</code>, or a
      noncomputable <code>Set.powerset</code> vs the enumerable <code>Finset.powerset</code>), kept
      visible rather than papered over.</p>
  </section>

  <p class="next"><b>Done:</b> a verified composite-evaluator proof-of-concept &mdash; a small deep
    embedding of the Wikifunctions composition language whose evaluator proves <code>Z13701</code>&#x27;s
    composite implementation <code>equals(gcd(a, b), 1)</code> equal to Mathlib&#x27;s
    <code>Nat.Coprime</code> (zero <code>sorry</code>; axioms = <code>propext</code>).
    <b>Next:</b> generalize the evaluator to the remaining composite functions, and a
    differential-testing harness that runs these oracles against the live Wikifunctions evaluator API.</p>

  <footer>
    <p>Wikifunctions data &copy; its contributors, available under
      <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>;
      Mathlib is <a href="https://github.com/leanprover-community/mathlib4">Apache-2.0</a>.
      WikiLean&#x27;s mappings are released under
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
