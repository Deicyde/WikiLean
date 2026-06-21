// Assembles the full article page around the engine-wrapped body. Mirrors
// render.py's PAGE_TEMPLATE + client_data, but links /assets/style.css and
// /assets/script.js externally (served as static assets) instead of inlining —
// the annotation wrapping in <main> is byte-identical to the static site.

import { htmlEscape } from "./html.js";
import type { Annotation } from "./types.js";

export const BASE_URL = "https://wikilean.jackmccarthy.org";
const MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs";

// Build a mathlib4_docs URL from a module path + decl name. `module`/`decl`
// are annotation-controlled and end up in an href attribute, so each path
// segment and the fragment must be URL-encoded to guarantee the result can
// never contain attribute-breaking characters (" < > space etc.). The "/"
// separators between module segments, the ".html" suffix, and the "#" fragment
// separator are kept literal. mathlib4_docs anchors use the raw dotted decl
// name in the fragment; encodeURIComponent leaves alphanumerics, ".", "_", "-"
// and "~" untouched, so a normal decl like "Ideal.IsPrime" is preserved exactly.
export function mathlibDocsUrl(module?: string | null, decl?: string | null): string | null {
  if (!module) return null;
  const rel = module.split(".").map(encodeURIComponent).join("/") + ".html";
  return decl ? `${MATHLIB_DOCS}/${rel}#${encodeURIComponent(decl)}` : `${MATHLIB_DOCS}/${rel}`;
}

interface ClientAnno {
  // Stable annotation id — lets the anonymous flag form target a specific
  // annotation (POST /api/flag/:slug). Inert in rendering.
  id?: string;
  status: string;
  label?: string;
  kind?: string;
  note?: string;
  proof_note?: string;
  provenance?: string;
  match_kind?: string;
  decl?: string;
  module?: string;
  mathlib_url?: string;
}

export function buildClientData(annotations: Annotation[]): Array<ClientAnno | null> {
  const out: Array<ClientAnno | null> = [];
  for (const a of annotations) {
    // Human-deletion tombstones (status="rejected") must not ship to
    // anonymous readers — they're unrendered noise and would leak vetoed
    // content. A null placeholder (not a filter) keeps the array index-
    // aligned with data-anno-indices in the wrapped HTML; tombstones are
    // never wrapped, so no index references the null, and script.js
    // .filter(Boolean)s its lookups anyway. Mirrored in render.py client_data.
    if (a.status === "rejected") {
      out.push(null);
      continue;
    }
    const m = a.mathlib ?? {};
    const decl = m.decl || a.decl;
    const module = m.module || a.module;
    const item: ClientAnno = { status: a.status };
    if (typeof a.id === "string") item.id = a.id;
    for (const k of ["label", "kind", "note", "proof_note", "provenance"] as const) {
      if (a[k]) item[k] = a[k] as string;
    }
    if (m.match_kind) item.match_kind = m.match_kind;
    if (decl) item.decl = decl;
    if (module) item.module = module;
    const url = mathlibDocsUrl(module, decl);
    if (url) item.mathlib_url = url;
    out.push(item);
  }
  return out;
}

// Embed JSON in <script> safely (mirrors render.py._safe_json_for_script).
function safeJsonForScript(obj: unknown): string {
  return JSON.stringify(obj).replaceAll("</", "<\\/");
}

export interface PageInput {
  slug: string;
  displayTitle: string;
  wikipediaTitle: string;
  body: string; // engine-wrapped <main> content
  annotations: Annotation[];
  matched: boolean[];
  wpHtml: string; // absolutized source, for the unannotated-math count
}

export function renderArticlePage(input: PageInput): string {
  const { slug, displayTitle, wikipediaTitle, body, annotations, matched, wpHtml } = input;

  // "rejected" (human-deletion tombstone) is deliberately absent from
  // `counts`, so tombstones never reach the header badges or `desc`.
  const counts = { formalized: 0, partial: 0, not_formalized: 0 } as Record<string, number>;
  for (const a of annotations) {
    if (a.status in counts) counts[a.status] += 1;
  }
  const nTotal = counts.formalized + counts.partial + counts.not_formalized;

  const nDisplayTotal = (wpHtml.match(/<span class="mwe-math-element mwe-math-element-block">/g) ?? []).length;
  let nDisplayAnnotated = 0;
  for (let i = 0; i < annotations.length; i++) {
    // Tombstones report matched=true from the wrap engine ("excluded, not an
    // anchor failure") but emit no wrap, so any math element they once
    // covered counts as unannotated again.
    if (annotations[i].status === "rejected") continue;
    if (matched[i] && annotations[i].anchor?.type === "math_alttext") nDisplayAnnotated += 1;
  }
  // Rendered as a header badge only when non-zero — "0 unannotated math" is
  // noise on fully-covered (or math-free) articles (W3 fix #12).
  const nUntouched = Math.max(0, nDisplayTotal - nDisplayAnnotated);

  const desc =
    nTotal > 0
      ? `${displayTitle} from Wikipedia, annotated with links into Mathlib4: ${counts.formalized} of ${nTotal} definitions, theorems, and proofs formalized in Lean (${counts.partial} partial).`
      : `${displayTitle} from Wikipedia, annotated with links into Mathlib4 / Lean.`;

  const title = htmlEscape(displayTitle, false);
  // Attribute positions (og:title content="...") need quotes escaped too;
  // <title>/text positions keep the quote-unescaped form (W3 fix #7).
  const titleAttr = htmlEscape(displayTitle, true);
  const descEsc = htmlEscape(desc, true);
  const canonical = `${BASE_URL}/${encodeURIComponent(slug)}`;
  const wpLink = encodeURIComponent(wikipediaTitle.replaceAll(" ", "_"));
  const data = safeJsonForScript(buildClientData(annotations));

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${title}</title>
<meta name="description" content="${descEsc}">
<link rel="canonical" href="${canonical}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="WikiLean">
<meta property="og:title" content="${titleAttr}">
<meta property="og:description" content="${descEsc}">
<meta property="og:url" content="${canonical}">
<meta name="twitter:card" content="summary">
<script>
/* Set the theme attr BEFORE the stylesheet parses so dark mode applies on
   first paint (no flash of light content). Priority: localStorage > OS
   preference > light default. The toggle in the header (script at the
   bottom of body) flips between "dark" / "light" and persists in
   localStorage. */
(function(){try{var s=localStorage.getItem("wl-theme");
var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
document.documentElement.dataset.theme=t;}catch(e){}})();
</script>
<link rel="stylesheet" href="/assets/style.css?v=8">
<style>
/* Use the article-stylesheet's tokens so the attribution footer recolors in
   dark mode along with the rest of the chrome. */
.wl-attribution { max-width: 1000px; margin: 24px auto 40px; padding: 12px 16px 0; border-top: 1px solid var(--line-strong); color: var(--muted); font-size: 12px; line-height: 1.5; }
.wl-attribution p { margin: 4px 0; }
.wl-attribution a { color: var(--accent); text-decoration: none; }
.wl-attribution a:hover { text-decoration: underline; }
</style>
</head>
<body class="show-all">
<header class="wl-header">
  <div class="wl-title">
    <a class="wl-brand" href="/">WikiLean</a>
    <span class="wl-sep">·</span>
    <span class="wl-article">${title}</span>
    <span class="wl-nav">
      <a class="wl-navlink" href="/about">About</a>
      <a class="wl-navlink" href="/${encodeURIComponent(slug)}/history">History</a>
      <a class="wl-wikilink" href="https://en.wikipedia.org/wiki/${wpLink}" target="_blank" rel="noopener">view on Wikipedia ↗</a>
      <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
    </span>
  </div>
  <div class="wl-controls">
    <span class="wl-coverage">
      <span class="wl-badge wl-formalized">${counts.formalized} formalized</span>
      <span class="wl-badge wl-partial">${counts.partial} partial</span>
      <span class="wl-badge wl-not_formalized">${counts.not_formalized} not formalized</span>
      ${nUntouched > 0 ? `<span class="wl-badge wl-untouched">${nUntouched} unannotated math</span>` : ""}
    </span>
    <span class="wl-toggles" role="group" aria-label="Filter annotations by status">
      <button data-mode="all" class="active" aria-pressed="true">All</button>
      <button data-mode="formalized" aria-pressed="false">Formalized only</button>
      <button data-mode="not_formalized" aria-pressed="false">Not-formalized only</button>
      <button data-mode="dim" aria-pressed="false">Dim unannotated</button>
    </span>
  </div>
</header>
<main class="wl-article-body">
${body}
</main>
<footer class="wl-attribution">
  <p>Article text from <a href="https://en.wikipedia.org/wiki/${wpLink}" target="_blank" rel="noopener">Wikipedia</a>, available under <a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noopener">CC BY-SA 4.0</a>.</p>
  <p>WikiLean annotations are released under <a href="https://creativecommons.org/publicdomain/zero/1.0/" target="_blank" rel="noopener">CC0</a>.</p>
</footer>
<div id="wl-tooltip" hidden></div>
<script>
window.__WL_ANNOTATIONS__ = ${data};
</script>
<script>
/* Theme toggle. Flips between explicit "dark" / "light" and persists in
   localStorage so the choice survives across articles. */
(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;
b.addEventListener("click",function(){var r=document.documentElement;
var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;
try{localStorage.setItem("wl-theme",n);}catch(e){}});})();
</script>
<script src="/assets/script.js?v=7"></script>
</body>
</html>
`;
}
