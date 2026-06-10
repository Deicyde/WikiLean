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

export function buildClientData(annotations: Annotation[]): ClientAnno[] {
  const out: ClientAnno[] = [];
  for (const a of annotations) {
    const m = a.mathlib ?? {};
    const decl = m.decl || a.decl;
    const module = m.module || a.module;
    const item: ClientAnno = { status: a.status };
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

  const counts = { formalized: 0, partial: 0, not_formalized: 0 } as Record<string, number>;
  for (const a of annotations) {
    if (a.status in counts) counts[a.status] += 1;
  }
  const nTotal = counts.formalized + counts.partial + counts.not_formalized;

  const nDisplayTotal = (wpHtml.match(/<span class="mwe-math-element mwe-math-element-block">/g) ?? []).length;
  let nDisplayAnnotated = 0;
  for (let i = 0; i < annotations.length; i++) {
    if (matched[i] && annotations[i].anchor?.type === "math_alttext") nDisplayAnnotated += 1;
  }
  const nUntouched = Math.max(0, nDisplayTotal - nDisplayAnnotated);

  const desc =
    nTotal > 0
      ? `${displayTitle} from Wikipedia, annotated with links into Mathlib4: ${counts.formalized} of ${nTotal} definitions, theorems, and proofs formalized in Lean (${counts.partial} partial).`
      : `${displayTitle} from Wikipedia, annotated with links into Mathlib4 / Lean.`;

  const title = htmlEscape(displayTitle, false);
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
<meta property="og:title" content="${title}">
<meta property="og:description" content="${descEsc}">
<meta property="og:url" content="${canonical}">
<meta name="twitter:card" content="summary">
<link rel="stylesheet" href="/assets/style.css?v=4">
<style>
.wl-attribution { max-width: 1000px; margin: 24px auto 40px; padding: 12px 16px 0; border-top: 1px solid #d8dee4; color: #57606a; font-size: 12px; line-height: 1.5; }
.wl-attribution p { margin: 4px 0; }
.wl-attribution a { color: #0969da; text-decoration: none; }
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
    </span>
  </div>
  <div class="wl-controls">
    <span class="wl-coverage">
      <span class="wl-badge wl-formalized">${counts.formalized} formalized</span>
      <span class="wl-badge wl-partial">${counts.partial} partial</span>
      <span class="wl-badge wl-not_formalized">${counts.not_formalized} not formalized</span>
      <span class="wl-badge wl-untouched">${nUntouched} unannotated math</span>
    </span>
    <span class="wl-toggles">
      <button data-mode="all" class="active">All</button>
      <button data-mode="formalized">Formalized only</button>
      <button data-mode="not_formalized">Not-formalized only</button>
      <button data-mode="dim">Dim unannotated</button>
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
<script src="/assets/script.js?v=4"></script>
</body>
</html>
`;
}
