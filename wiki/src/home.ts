// Public entry pages. Reader counts and provenance live with the reader, where
// their Wikipedia revisions and searched library versions can be understood — with
// one exception: the home page shows ONE real example (a passage of a real article
// beside the Lean declaration that formalizes it) and the published build's own
// totals, both read from the articles store at request time (articles.ts
// articlesExample) and never written in this repository. No store, no block.
import { htmlEscape } from "./engine/html.js";
import { siteNav, SITE_TOOLS } from "./site-nav.js";
import type { ArticlesExample } from "./articles.js";

export interface SitemapRow { slug: string; updatedAt: number }
const SITE_ORIGIN = "https://wikilean.jackmccarthy.org";
const esc = (s: string) => htmlEscape(s, true);

// One small shell for home, About and Contribute; no corpus or account data.
function publicPage(title: string, path: string, description: string, body: string): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — ${esc(title)}</title>
<meta name="description" content="${esc(description)}">
<link rel="canonical" href="${SITE_ORIGIN}${path}">
<script>(function(){try{var s=localStorage.getItem("wl-theme");document.documentElement.dataset.theme=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");}catch(e){}})();</script>
<style>
:root { color-scheme:light; --paper:#f7f4ee; --surface:#fffdf9; --ink:#1f1d1a; --muted:#5f594e; --line:#d8d0bd; --accent:#1a4b8c; }
[data-theme="dark"] { color-scheme:dark; --paper:#1a1816; --surface:#232020; --ink:#ebe5d8; --muted:#b0a798; --line:#4d4742; --accent:#8fb4e8; }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
a { color:var(--accent); text-underline-offset:3px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:4px; }
.wl-header { background:var(--surface); border-bottom:1px solid var(--line); padding:16px max(24px,calc((100% - 1080px)/2)); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px 32px; }
.wl-brand,h1,h2 { font-family:Charter,'Iowan Old Style',Georgia,serif; }
.wl-brand { font-size:24px; font-weight:700; color:var(--ink); text-decoration:none; }
.wl-nav { display:flex; flex-wrap:wrap; align-items:center; gap:12px 24px; }
.wl-navlink { font-size:.95rem; text-decoration:none; }
.wl-navlink:hover { text-decoration:underline; }
.wl-navlink[aria-current] { color:var(--ink); font-weight:600; }
.wl-theme-toggle { border:1px solid var(--line); background:var(--surface); color:var(--ink); border-radius:50%; min-width:36px; min-height:36px; cursor:pointer; }
main { max-width:960px; margin:auto; padding:64px 24px 48px; }
h1 { max-width:760px; font-size:clamp(2.2rem,5vw,3.6rem); line-height:1.13; font-weight:500; margin:0 0 24px; letter-spacing:-.02em; }
h2 { font-size:1.5rem; line-height:1.3; margin:32px 0 12px; }
p,ol,ul { max-width:720px; }
.lead { color:var(--muted); font-size:1.2rem; line-height:1.6; max-width:640px; }
.actions { display:flex; flex-wrap:wrap; align-items:center; gap:16px 24px; margin:28px 0; }
.button { display:inline-block; padding:10px 22px; background:var(--accent); color:var(--paper); border-radius:5px; text-decoration:none; font-weight:600; }
.button:hover { text-decoration:underline; }
.paths { display:grid; grid-template-columns:1fr 1fr; gap:32px; border-top:1px solid var(--line); margin-top:48px; }
.paths h2 { margin-top:24px; }
.note { color:var(--muted); font-size:.92rem; }
/* One real example from the published store: the passage, then the Lean declaration
   that formalizes it (the Lean code leads the card, as it does in the reader). */
.example { border-top:1px solid var(--line); margin-top:40px; }
.example h2 { margin-top:24px; }
.example-pair { display:grid; grid-template-columns:1fr 1fr; gap:20px; align-items:start; max-width:720px; }
.example-text { margin:0; }
.example-text blockquote { margin:0; padding:0 0 0 14px; border-left:3px solid var(--line); font-family:Charter,'Iowan Old Style',Georgia,serif; font-size:1.05rem; }
.example-text figcaption { margin-top:8px; color:var(--muted); font-size:.85rem; }
.example-card { padding:12px 14px; border:1px solid var(--line); border-radius:6px; background:var(--surface); }
.example-card code { display:block; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9rem; overflow-wrap:anywhere; }
.example-card .note { margin:8px 0 0; }
li { margin:10px 0; }
footer { max-width:960px; margin:auto; border-top:1px solid var(--line); padding:24px 24px 40px; color:var(--muted); font-size:.85rem; }
footer p { margin:6px 0; }
.skip { position:absolute; left:16px; top:-100px; padding:8px; background:var(--surface); }
.skip:focus { top:8px; }
@media(max-width:600px) { main { padding-top:40px; } .paths, .example-pair { grid-template-columns:1fr; } .paths { gap:0; } .wl-nav { gap:12px 18px; } }
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><nav class="wl-nav" aria-label="Site">${siteNav(path)}<button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button></nav></header>
<main id="main">${body}</main>
<footer>
<p>${SITE_TOOLS}</p>
<p><a href="https://github.com/Deicyde/WikiLean">Source on GitHub</a> · A project by <a href="https://jackmccarthy.org">Jack McCarthy</a></p>
<p>Wikipedia text: <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>. WikiLean annotations: <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>. Lean code retains its library’s licence.</p>
</footer>
<script>(function(){var b=document.getElementById("wl-theme-toggle");b.addEventListener("click",function(){var r=document.documentElement;var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;try{localStorage.setItem("wl-theme",n);}catch(e){}});})();</script>
</body>
</html>`;
}

// How a relation reads on the example card, in the reader's own words.
const EXAMPLE_RELATION: Record<string, string> = {
  exactMatch: "states this exactly",
  closeMatch: "states this up to a convention difference",
  broadMatch: "states a more general result",
  narrowMatch: "states a special case",
  hasPart: "states part of this",
  relatedMatch: "states something related",
};

// One real passage beside its Lean declaration, and the build's own totals. Every
// value is the store's; an unavailable store leaves the whole block out.
function exampleBlock(example: ArticlesExample | null): string {
  if (!example) return "";
  const relation = EXAMPLE_RELATION[example.relation] ?? "matches this";
  const share = example.passages > 0 ? Math.round((100 * example.formalized) / example.passages) : 0;
  return `
<section class="example" aria-labelledby="example-h">
<h2 id="example-h">What a link looks like</h2>
<div class="example-pair">
<figure class="example-text">
<blockquote>${esc(example.quote)}</blockquote>
<figcaption>From <a href="${esc(example.href)}">${esc(example.title)}</a>, Wikipedia revision ${esc(String(example.revision))}</figcaption>
</figure>
<div class="example-card">
<code>${esc(example.declaration)}</code>
<p class="note">${esc(example.library)} — ${esc(relation)}</p>
</div>
</div>
<p class="note">${esc(String(example.articles))} articles · ${esc(String(example.passages))} passages checked · ${esc(String(example.formalized))} formalized (${esc(String(share))}%).</p>
</section>`;
}

export function landingPage(example: ArticlesExample | null = null): string {
  return publicPage("Connect mathematics to Lean", "/", "Find Lean formalizations of Wikipedia mathematics, check the matches, and contribute missing links.", `
<h1>Connect mathematics<br>to Lean.</h1>
<p class="lead">Read a mathematical statement. Find its formalization. Help make the connection clearer for the next reader.</p>
<p>WikiLean links passages in Wikipedia’s mathematics articles to declarations in Lean libraries. Every match is something you can inspect, question, and improve.</p>
<div class="actions"><a class="button" href="/articles">Browse articles</a><a href="/contribute">Start contributing →</a></div>
<p class="note">Matches are preliminary. A Lean proof checks the formal statement; people still need to check that it matches the article.</p>
${exampleBlock(example)}
<div class="paths">
<section><h2>Improve a link</h2><p>Confirm a match, report a problem, or add a missing declaration. Contributions are public and attributed to your account.</p><a href="/contribute">How to contribute</a></section>
<section><h2>Explore the connections</h2><p>The Brain connects mathematical concepts, formal code, reference databases, and literature.</p><a href="/brain">Explore the Brain →</a></section>
</div>`);
}

export function contributePage(): string {
  return publicPage("Contribute", "/contribute", "Help connect mathematical statements to Lean: check a match, add a tag, or contribute with your own agent.", `
<h1>Help connect the next statement.</h1>
<p class="lead">Start with an article you know. One careful correction is a useful contribution.</p>
<h2>Check or add a link</h2>
<ol>
<li><a href="/articles">Choose an article</a> and select a highlighted passage to see its Lean matches.</li>
<li><a href="/login">Sign in</a>. Use <b>Confirm</b> for a correct match, or <b>Report</b> if it is wrong or uncertain.</li>
<li>To add a match, choose <b>Add a tag</b>, select the library and declaration, and explain how closely it matches. Accept the CC0 contribution agreement when prompted.</li>
</ol>
<p>A saved tag is public immediately and attributed to your account. You can update it or remove it with a reason. Full tag history, diffs, and revert are still planned.</p>
<p>Check the statement’s assumptions and conclusion. A related theorem is useful, but should be labelled as related rather than an exact match. “Not found” describes the libraries and versions searched, not all of mathematics.</p>
<div class="actions"><a class="button" href="/articles">Find an article</a></div>
<h2>Contribute with an agent</h2>
<p>Create an agent account at <a href="/agents">Manage agents</a>. Its contributions carry its own identity, linked to you as its owner. Use your own model account and spending limits; never share your model API key with WikiLean.</p>
<p>For the tag API, Brain contributions, and running the article pipeline on another computer, see the <a href="https://github.com/Deicyde/WikiLean/blob/main/docs/START-CONTRIBUTING.md">contributor guide</a>. Your laptop can <a href="https://github.com/Deicyde/WikiLean/blob/main/docs/INTAKE.md#submit-from-your-laptop">submit a sealed pipeline result directly</a> using its agent token. You get a receipt with the validation result; accepted submissions await publication.</p>
<h2>Know what you are sharing</h2>
<p>Contributions are public under CC0. WikiLean also studies how people and AI improve a shared database; research exports use pseudonyms. Read the <a href="https://github.com/Deicyde/WikiLean/blob/main/CONTRIBUTING.md#data--research-notice">data and research notice</a> before contributing.</p>`);
}

export function aboutPage(): string {
  return publicPage("About", "/about", "What WikiLean does, how to read its formalization links, and what remains uncertain.", `
<h1>Mathematics, with links<br>you can check.</h1>
<p class="lead">WikiLean is a shared collection of connections between mathematical writing and formal proofs.</p>
<p>The goal is a place where anyone can contribute formalization links, discuss their accuracy, and build on each other’s work. Today you can read annotated articles, check matches, and add or correct tags.</p>
<h2>What a link means</h2>
<p>The <a href="/articles">article reader</a> connects Wikipedia passages to Lean declarations across a pinned set of libraries, including Mathlib. A match can be exact, more general, a special case, partial, or simply related. The reader records that distinction and links to the source.</p>
<p>A proved declaration is not proof that the prose and the declaration mean the same thing. That connection is a judgment and can be wrong. A “not formalized” result means no match was found in the libraries and versions searched.</p>
<h2>Keep the evidence with the claim</h2>
<p>Articles refer to specific Wikipedia revisions and library commits. Build details, source links, and audit information remain available in the reader. Initial matches are AI-assisted and preliminary; community checks help improve them.</p>
<p>Canonical annotations use the <a href="https://github.com/Deicyde/WikiLean/blob/main/docs/COMPANION-NATIVE.md">Literature Links</a> format to identify sources and targets. The <a href="/brain">Brain</a> brings those connections together with concepts, reference databases, and literature.</p>
<h2>Contribute and review</h2>
<p>Human tags appear immediately and show their author. You can confirm or report a match, or add, change, and remove tags. Agent accounts are identified separately and linked to their owners. See <a href="/contribute">how to contribute</a> for the current workflow and its limits.</p>
<p>Maintainers can moderate contributions and pause editing during maintenance. The older annotation system’s <a href="/proposals">proposal queue</a>, <a href="/flags">reader reports</a>, and <a href="/recent-changes">change log</a> remain available; they are separate from the reader’s community tags.</p>
<h2>Open data and research</h2>
<p>WikiLean studies how people and AI moderate a shared mathematical database. Contributions and their attribution are public; research exports use pseudonyms and exclude email addresses and IP addresses. See the <a href="https://github.com/Deicyde/WikiLean/blob/main/CONTRIBUTING.md#data--research-notice">research notice</a>.</p>
<p>Developers can use the <a href="/brain/api">Brain API</a>, <a href="/mcp">MCP tools</a>, and <a href="/concepts">concept dataset</a>. Project plans and technical details live in the <a href="https://github.com/Deicyde/WikiLean">repository</a>.</p>`);
}

export function sitemapXml(rows: SitemapRow[]): string {
  // Homepage root first, at top priority — it's the page that owns the brand
  // query. lastmod tracks the most recently updated article (the homepage
  // re-renders whenever any article changes).
  const newest = rows.reduce((m, r) => (r.updatedAt > m ? r.updatedAt : m), 0);
  const rootMod = newest ? `<lastmod>${new Date(newest).toISOString().slice(0, 10)}</lastmod>` : "";
  const rootUrl = `  <url><loc>${SITE_ORIGIN}/</loc>${rootMod}<priority>1.0</priority></url>`;
  // Flagship pages (the old /map + /article-graph routes are deleted — not listed;
  // /articles is the formalization reader since 2026-11-05 and asks not to be
  // indexed, src/articles.ts ARTICLES_ROBOTS, so it is not listed either).
  const staticUrls = ["brain", "mcp", "concepts", "about"].map(
    (p) => `  <url><loc>${SITE_ORIGIN}/${p}</loc><priority>0.8</priority></url>`,
  );
  const urls = [
    rootUrl,
    ...staticUrls,
    ...[...rows]
      .sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
      .map(
        (r) =>
          // <loc> must be a valid URL: percent-encode the slug, then XML-escape
          // (W3 fix #10).
          `  <url><loc>${SITE_ORIGIN}/${esc(encodeURIComponent(r.slug))}</loc>` +
          `<lastmod>${new Date(r.updatedAt).toISOString().slice(0, 10)}</lastmod>` +
          `<priority>0.6</priority></url>`,
      ),
  ];
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.join("\n") +
    (urls.length ? "\n" : "") +
    `</urlset>\n`
  );
}
