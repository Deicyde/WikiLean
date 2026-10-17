// Server-rendered HTML for the formalization atlas (docs/ATLAS.md, "UI"):
// the /atlas dashboard, one article's coverage page, the universe page, the
// 503/404 message page, and the compact box injected into article pages.
//
// Every value from the store is data: text goes through htmlEscape, every href
// is either built here from validated parts or parsed and required to be
// https, and no element carries an inline event handler. The only script on
// these pages besides the shared theme bootstrap and header search is
// /assets/atlas.js (site/assets/atlas.js), which writes the DOM through
// textContent and createElement only. Status is never colour alone: each
// status has an icon, a word and its own stroke pattern (atlas.css).

import { htmlEscape } from "./engine/html.js";
import { SEARCHBOX_CSS, SEARCHBOX_HTML, SEARCHBOX_SCRIPT } from "./engine/searchbox.js";
import type { AuthUser } from "./auth.js";
import type {
  AtlasArticle, AtlasCurrent, AtlasLocation, AtlasPassage, AtlasUniverse, IndexEntry, SummaryStatus,
} from "./atlas.js";
import { libraryInfo, sourceUrl, treeUrl, type LibraryInfo, type UniverseLibrary } from "./libraries.js";

// Asset versions: bump with any change to site/assets/atlas.{css,js}.
export const ATLAS_CSS = "/assets/atlas.css?v=1";
export const ATLAS_JS = "/assets/atlas.js?v=1";
const STYLE_CSS = "/assets/style.css?v=9";

const text = (s: string) => htmlEscape(s, false);
const attr = (s: string) => htmlEscape(s, true);

export type DashboardRow = IndexEntry & { href: string };

// ---- vocabulary ------------------------------------------------------------

const STATUS: Record<SummaryStatus, { label: string; icon: string; plural: string }> = {
  formalized: { label: "Formalized", icon: "✓", plural: "formalized" },
  statement_only: { label: "Statement only", icon: "◐", plural: "statement only" },
  partial: { label: "Partial", icon: "◔", plural: "partial" },
  not_formalized: { label: "Not formalized", icon: "✕", plural: "not formalized" },
  unresolved: { label: "Undecided", icon: "?", plural: "undecided" },
};
const STATUS_ORDER: SummaryStatus[] = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"];

const RELATION_WORD: Record<string, string> = {
  exactMatch: "exact match",
  closeMatch: "close match",
  broadMatch: "more general",
  narrowMatch: "special case",
  hasPart: "states part",
  relatedMatch: "related",
};
const RELATION_PHRASE: Record<string, string> = {
  exactMatch: "states this exactly",
  closeMatch: "states this up to a convention difference",
  broadMatch: "states a more general result",
  narrowMatch: "states a special case",
  hasPart: "states part of this",
  relatedMatch: "states something related",
};
const PROOF: Record<string, { label: string; phrase: string; alert: boolean }> = {
  proved: { label: "proved", phrase: "proved", alert: false },
  definition: { label: "definition", phrase: "a definition", alert: false },
  statement_only: { label: "statement only", phrase: "statement only: the proof is not finished", alert: true },
  wanted: { label: "wanted", phrase: "wanted: stated as a goal, with no proof", alert: true },
  axiom_dependent: { label: "axiom-dependent", phrase: "depends on an axiom beyond the standard three", alert: true },
};
const CHALLENGE: Record<string, string> = {
  survived: "a challenger checked it",
  downgraded: "a challenger downgraded it",
  not_required: "not challenged",
  sampled_out: "not challenged (sampled out)",
};
const IMPORTANCE_ORDER = ["top", "high", "mid", "low", "bottom", "na", "unknown"];

// A status outside the vocabulary (a corrupt store) renders as undecided
// rather than throwing; the builder never writes one.
function asStatus(value: string | null | undefined): SummaryStatus {
  return STATUS_ORDER.includes(value as SummaryStatus) ? (value as SummaryStatus) : "unresolved";
}

function proofOf(status: string) {
  return PROOF[status] ?? { label: status, phrase: status, alert: true };
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function importanceLabel(value: string | null | undefined): string {
  if (!value || value === "unknown") return "Not assessed";
  if (value === "na") return "N/A";
  return capital(value);
}

function sha7(commit: string): string {
  return commit.slice(0, 7);
}

function httpsUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function wikipediaRevisionUrl(pageid: number, revision: number): string {
  return `https://en.wikipedia.org/w/index.php?curid=${pageid}&oldid=${revision}`;
}

// The passage in context: the record's own subject URI (the pinned revision)
// plus a text fragment, so the browser scrolls to and highlights the quote.
function inContextUrl(passage: AtlasPassage): string | null {
  const base = httpsUrl(passage.subject_uri);
  if (!base || !base.startsWith("https://en.wikipedia.org/")) return null;
  const fragment = encodeURIComponent(passage.quote.exact).replace(/-/g, "%2D");
  return `${base.replace(/#.*$/, "")}#:~:text=${fragment}`;
}

// Namespace faint, leaf bold, with break opportunities after "." and "_" so a
// long Lean name wraps inside a narrow card.
function declName(name: string): string {
  const cut = name.lastIndexOf(".");
  const breakable = (s: string) => text(s).replace(/([._])/g, "$1<wbr>");
  const ns = cut > 0 ? `<span class="atlas-ns">${breakable(name.slice(0, cut + 1))}</span>` : "";
  return `<code class="atlas-decl">${ns}<b>${breakable(cut > 0 ? name.slice(cut + 1) : name)}</b></code>`;
}

function pill(status: SummaryStatus, sentence: string): string {
  const meta = STATUS[status];
  return `<span class="atlas-pill" data-status="${status}" title="${attr(sentence)}">` +
    `<span class="atlas-ico" aria-hidden="true">${meta.icon}</span>${text(meta.label)}` +
    `<span class="atlas-sr">. ${text(sentence)}</span></span>`;
}

// ---- shell -----------------------------------------------------------------

function shell(opts: { title: string; description: string; canonical?: string; main: string; script?: boolean }): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${text(opts.title)}</title>
<meta name="description" content="${attr(opts.description)}">
${opts.canonical ? `<link rel="canonical" href="${attr(opts.canonical)}">\n` : ""}<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<link rel="stylesheet" href="${STYLE_CSS}">
<link rel="stylesheet" href="${ATLAS_CSS}">
<style>${SEARCHBOX_CSS}</style>
</head>
<body class="atlas-body">
<header class="wl-header">
  <div class="wl-title">
    <a class="wl-brand" href="/">WikiLean</a>
    <span class="wl-sep">·</span>
    <a class="wl-article atlas-home" href="/atlas">Formalization atlas</a>
    <span class="wl-nav">
      ${SEARCHBOX_HTML}
      <a class="wl-navlink" href="/articles">Articles</a>
      <a class="wl-navlink" href="/atlas" aria-current="page">Atlas</a>
      <a class="wl-navlink" href="/brain">Brain</a>
      <a class="wl-navlink" href="/recent-changes">Recent changes</a>
      <a class="wl-navlink" href="/stats">Stats</a>
      <a class="wl-navlink" href="/about">About</a>
      <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
    </span>
  </div>
</header>
${opts.main}
<footer class="atlas-foot">
  <p>Quoted passages are excerpts of <a href="https://en.wikipedia.org/">Wikipedia</a> articles, available under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>; the quote fields of the JSON and JSONL downloads stay under that licence.</p>
  <p>The mapping (which passage is formalized where) is released under <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>, like WikiLean&#x27;s annotations. Links are found by an AI pipeline and checked by a second AI pass; people can confirm or report each one.</p>
</footer>
${SEARCHBOX_SCRIPT}
${opts.script === false ? "" : `<script src="${ATLAS_JS}"></script>\n`}</body>
</html>
`;
}

export function atlasMessagePage(title: string, message: string, detail: string): string {
  return shell({
    title,
    description: message,
    main: `<main class="wl-atlas atlas-message">\n<h1 class="atlas-h1">${text(title)}</h1>\n<p>${text(message)}</p>\n` +
      (detail ? `<p class="atlas-muted">${text(detail)}</p>\n` : "") +
      `<p><a href="/atlas">Formalization atlas</a> · <a href="/articles">Articles</a></p>\n</main>`,
  });
}

// ---- shared pieces -----------------------------------------------------------

interface LibraryView {
  id: string;
  info: LibraryInfo;
  commit: string | null;
}

function libraryViews(universe: AtlasUniverse | null, extra: Iterable<{ library: string; repo: string; commit: string }>): Map<string, LibraryView> {
  const views = new Map<string, LibraryView>();
  for (const lib of universe?.libraries ?? []) {
    views.set(lib.id, { id: lib.id, info: libraryInfo(lib.repo, lib), commit: lib.commit });
  }
  for (const loc of extra) {
    if (!views.has(loc.library)) views.set(loc.library, { id: loc.library, info: libraryInfo(loc.repo, null), commit: loc.commit });
  }
  return views;
}

function statusBar(counts: Record<SummaryStatus, number>, noun: string): string {
  const total = STATUS_ORDER.reduce((sum, s) => sum + (counts[s] ?? 0), 0);
  if (total === 0) return "";
  const label = STATUS_ORDER.filter((s) => counts[s]).map((s) => `${counts[s]} ${STATUS[s].plural}`).join(", ");
  const segments = STATUS_ORDER.filter((s) => counts[s]).map((s) => {
    const pct = ((counts[s] / total) * 100).toFixed(2);
    return `<span class="atlas-seg" data-status="${s}" style="width:${pct}%" title="${attr(`${counts[s]} ${STATUS[s].plural}`)}"></span>`;
  }).join("");
  return `<div class="atlas-bar" role="img" aria-label="${attr(`${total} ${noun}: ${label}`)}">${segments}</div>`;
}

function statusSentence(passage: AtlasPassage, libs: Map<string, LibraryView>, article: AtlasArticle): string {
  const status = asStatus(passage.status);
  if (status === "unresolved" || passage.outcome === "unresolved") {
    return "Undecided: the pipeline could not settle this passage, so it is not counted as not formalized.";
  }
  if (status === "not_formalized") {
    const n = passage.absence?.libraries ?? article.universe.libraries;
    return `Not formalized: not found in the ${n} libraries of universe ${article.universe.id} (searched ${article.universe.frozen}).`;
  }
  const best = passage.locations.find((loc) => loc.relation !== "relatedMatch");
  const meta = STATUS[status];
  if (!best) return `${meta.label}.`;
  const name = libs.get(best.library)?.info.name ?? best.library;
  return `${meta.label}: ${name} ${RELATION_PHRASE[best.relation] ?? best.relation} (${proofOf(best.proof_status).phrase}).`;
}

interface CardContext {
  libs: Map<string, LibraryView>;
  user: AuthUser | null;
  verdicts: Map<string, string>;
  loginHref: string;
}

function locationCard(loc: AtlasLocation, ctx: CardContext): string {
  const view = ctx.libs.get(loc.library);
  const info = view?.info ?? libraryInfo(loc.repo, null);
  const src = sourceUrl(info, loc);
  const proof = proofOf(loc.proof_status);
  const short = sha7(loc.commit);
  const relation = RELATION_WORD[loc.relation] ?? loc.relation;
  const lines = loc.end_line && loc.end_line !== loc.line ? `lines ${loc.line}–${loc.end_line}` : `line ${loc.line}`;
  const verdict = ctx.verdicts.get(loc.location_key);
  const feedback = ctx.user
    ? `<div class="atlas-fb" data-location-key="${attr(loc.location_key)}">` +
      `<button type="button" class="atlas-fb-btn" data-verdict="confirm" aria-pressed="${verdict === "confirm"}">` +
      `<span aria-hidden="true">✓ </span>Confirm</button>` +
      `<button type="button" class="atlas-fb-btn" data-verdict="report" aria-pressed="${verdict === "report"}">` +
      `<span aria-hidden="true">⚑ </span>Report</button>` +
      `<span class="atlas-fb-msg" role="status">${verdict ? `You ${verdict === "confirm" ? "confirmed" : "reported"} this link.` : ""}</span></div>`
    : `<a class="atlas-fb-signin" href="${attr(ctx.loginHref)}">Sign in to confirm or report</a>`;
  return `<article class="atlas-card" data-library="${attr(loc.library)}" data-relation="${attr(loc.relation)}" data-proof="${attr(loc.proof_status)}">
<div class="atlas-card-top">
<span class="atlas-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span>
<code class="atlas-commit" title="${attr(`commit ${loc.commit}`)}">@${text(short)}</code>
<span class="atlas-rel" title="${attr(RELATION_PHRASE[loc.relation] ?? loc.relation)}">${text(relation)}</span>
<span class="atlas-ps${proof.alert ? " is-alert" : ""}" data-proof="${attr(loc.proof_status)}" title="${attr(capital(proof.phrase))}">${proof.alert ? `<span aria-hidden="true">! </span>` : ""}${text(proof.label)}</span>
${src ? `<a class="atlas-src" href="${attr(src)}" rel="noopener" target="_blank">Source @${text(short)}<span aria-hidden="true"> ↗</span><span class="atlas-sr"> (opens ${text(info.name)} at commit ${text(short)}, ${text(lines)})</span></a>` : `<span class="atlas-src is-none">No source link</span>`}
</div>
<p class="atlas-decl-line">${declName(loc.declaration)}</p>
<p class="atlas-module">${text(loc.module)} · ${text(lines)}</p>
${loc.note ? `<p class="atlas-note"><span class="atlas-k">Convention:</span> ${text(loc.note)}</p>\n` : ""}<div class="atlas-card-foot">
<span class="atlas-prov">AI pipeline · rank ${loc.rank} · ${text(loc.confidence)} confidence · ${text(CHALLENGE[loc.challenge] ?? loc.challenge)}</span>
${feedback}
</div>
</article>`;
}

function passageBlock(passage: AtlasPassage, article: AtlasArticle, ctx: CardContext): string {
  const status = passage.outcome === "unresolved" ? "unresolved" : asStatus(passage.status);
  const sentence = statusSentence(passage, ctx.libs, article);
  const strong = passage.locations.filter((loc) => loc.relation !== "relatedMatch");
  const related = passage.locations.filter((loc) => loc.relation === "relatedMatch");
  const libraries = [...new Set(strong.map((loc) => loc.library))];
  const hex = /#([0-9a-f]{8,16})$/.exec(passage.item)?.[1] ?? "";
  const eyebrow = [capital(passage.kind), passage.prominence].filter(Boolean).join(" · ");
  const section = passage.section?.length ? passage.section.join(" › ") : "";
  const context = inContextUrl(passage);
  const quote = passage.quote;
  const parts: string[] = [];
  parts.push(`<li class="atlas-passage" id="p-${hex}" data-status="${status}" data-libraries="${attr(libraries.join(" "))}">`);
  parts.push(`<div class="atlas-p-head"><span class="atlas-eyebrow">${text(eyebrow)}</span>` +
    (section ? `<span class="atlas-section">${text(section)}</span>` : "") + pill(status, sentence) + `</div>`);
  parts.push(`<blockquote class="atlas-quote">` +
    (quote.prefix ? `<span class="atlas-affix">…${text(quote.prefix)}</span>` : "") +
    `<mark>${text(quote.exact)}</mark>` +
    (quote.suffix ? `<span class="atlas-affix">${text(quote.suffix)}${/[.!?:;]\s*$/.test(quote.suffix) ? "" : "…"}</span>` : "") +
    `</blockquote>`);
  if (context) parts.push(`<p class="atlas-context"><a href="${attr(context)}" rel="noopener" target="_blank">Read it in context on Wikipedia<span aria-hidden="true"> ↗</span></a> <span class="atlas-muted">${text(passage.item)}</span></p>`);
  if (strong.length) {
    parts.push(`<div class="atlas-cards">${strong.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
  if (passage.absence) {
    const a = passage.absence;
    parts.push(`<p class="atlas-absence"><span class="atlas-ico" aria-hidden="true">✕</span>` +
      `Searched ${a.libraries} libraries in universe <a href="/atlas/universe/${attr(a.universe_sha256)}">${text(a.universe)}</a> ` +
      `(${text(article.universe.frozen)}) — not found. ` +
      `<span class="atlas-muted">The search receipt lists ${a.receipt.queries} queries and ${a.receipt.candidates} candidates examined; ${text(CHALLENGE[a.challenge] ?? a.challenge)}.</span></p>`);
  }
  if (related.length) {
    parts.push(`<p class="atlas-related-h">Related, but states something else</p>`);
    parts.push(`<div class="atlas-cards is-related">${related.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
  if (passage.outcome === "unresolved") {
    parts.push(`<p class="atlas-undecided"><span class="atlas-ico" aria-hidden="true">?</span>Undecided — the pipeline could not settle whether this passage is formalized. It is not counted as not formalized.</p>`);
  }
  parts.push(`</li>`);
  return parts.join("\n");
}

function chip(filter: string, label: string, count: number, title: string, status?: SummaryStatus, icon?: string): string {
  // A filter that would show nothing is not offered: a zero count is inert.
  if (count === 0) {
    return `<span class="atlas-chip is-static is-zero"${status ? ` data-status="${status}"` : ""}>` +
      (icon ? `<span class="atlas-ico" aria-hidden="true">${icon}</span>` : "") +
      `${text(label)} <span class="atlas-n">0</span></span>`;
  }
  return `<button type="button" class="atlas-chip" data-filter="${attr(filter)}"${status ? ` data-status="${status}"` : ""} aria-pressed="false" title="${attr(title)}">` +
    (icon ? `<span class="atlas-ico" aria-hidden="true">${icon}</span>` : "") +
    `${text(label)} <span class="atlas-n">${count}</span></button>`;
}

// ---- the article coverage page ---------------------------------------------

export interface ArticlePageInput {
  article: AtlasArticle;
  universe: AtlasUniverse | null;
  user: AuthUser | null;
  verdicts: Map<string, string>;
  wikileanSlug: string | null;
  selfHref: string;
}

export function atlasArticlePage(input: ArticlePageInput): string {
  const { article, universe, user, verdicts, wikileanSlug, selfHref } = input;
  const libs = libraryViews(universe, article.passages.flatMap((p) => p.locations));
  const ctx: CardContext = { libs, user, verdicts, loginHref: `/login?returnTo=${encodeURIComponent(selfHref)}` };
  const summary = article.summary;
  const n = summary.passages;
  const assessment = article.assessment;
  const assessed = assessment && (assessment.class || assessment.importance)
    ? `WikiProject Mathematics: ${[assessment.class ? `${assessment.class}-class` : "", assessment.importance ? `${importanceLabel(assessment.importance)} importance` : ""].filter(Boolean).join(", ")}`
    : "";
  const statusChips = STATUS_ORDER.map((s) => chip(`status:${s}`, STATUS[s].label, summary.by_status[s] ?? 0,
    `Show only ${STATUS[s].plural} passages`, s, STATUS[s].icon)).join("");
  const libraryChips = [...libs.values()]
    .filter((view) => summary.by_library[view.id])
    .map((view) => {
      const counts = summary.by_library[view.id];
      const commit = view.commit ? ` at commit ${sha7(view.commit)}` : "";
      if (!counts.passages) {
        return `<span class="atlas-chip is-static" title="${attr(`${view.info.name}${commit}: ${counts.related} related declaration${counts.related === 1 ? "" : "s"} only`)}">${text(view.info.short)} <span class="atlas-n">related only</span></span>`;
      }
      return chip(`library:${view.id}`, view.info.short, counts.passages,
        `${view.info.name}${commit}: ${counts.passages} passage${counts.passages === 1 ? "" : "s"}, ${counts.locations} location${counts.locations === 1 ? "" : "s"}. Show only those passages.`);
    }).join("");
  const searched = universe
    ? `Searched <a href="/atlas/universe/${attr(article.universe.sha256)}">universe ${text(article.universe.id)}</a>: ${article.universe.libraries} libraries, frozen ${text(article.universe.frozen)}${universe.excluded.length ? `, ${universe.excluded.length} excluded` : ""}`
    : `Universe ${text(article.universe.id)} (${article.universe.libraries} libraries, frozen ${text(article.universe.frozen)})`;
  const main = `<main class="wl-atlas" id="atlas-article" data-pageid="${article.pageid}" data-campaign="${attr(article.campaign)}">
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="atlas-h1">${text(article.title)}</h1>
<p class="atlas-meta">Wikipedia revision <a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}" rel="noopener">${article.revision}</a>${assessed ? ` · ${text(assessed)}` : ""}${wikileanSlug ? ` · <a href="/${attr(encodeURIComponent(wikileanSlug))}">Annotated article on WikiLean</a>` : ""}</p>
<p class="atlas-meta">${searched} · campaign ${text(article.campaign)}</p>
<section class="atlas-strip" aria-label="Coverage of this article">
<p class="atlas-strip-lead">${n} passage${n === 1 ? "" : "s"} checked${summary.locations ? `, ${summary.locations} Lean location${summary.locations === 1 ? "" : "s"}` : ""}${summary.related ? ` (and ${summary.related} related declaration${summary.related === 1 ? "" : "s"})` : ""}.</p>
${statusBar(summary.by_status, "passages")}
<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-status">Status</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-status">${statusChips}</div></div>
${libraryChips ? `<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-where">Where</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-where">${libraryChips}</div></div>` : ""}
<p class="atlas-filter-note" role="status"><span class="atlas-filter-text"></span> <button type="button" class="atlas-clear" hidden>Show all passages</button></p>
</section>
${n ? `<ol class="atlas-passages">\n${article.passages.map((p) => passageBlock(p, article, ctx)).join("\n")}\n</ol>` : `<p>No formal passages were found in this revision.</p>`}
<p class="atlas-data">Data: <a href="/api/atlas/article/${article.pageid}">view model (JSON)</a> · <a href="/api/atlas/records/${article.pageid}">Literature Links records (JSONL)</a> · record set <code title="${attr(article.record_set)}">${text(article.record_set.slice(0, 12))}</code></p>
${user ? `<p class="atlas-muted atlas-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they score the pipeline.</p>` : ""}
</main>`;
  const description = `Where ${article.title} is formalized in Lean: ${summary.by_status.formalized} of ${n} passages formalized.`;
  return shell({ title: `Atlas · ${article.title}`, description, canonical: `https://wikilean.jackmccarthy.org${selfHref}`, main });
}

// ---- the dashboard -----------------------------------------------------------

export interface DashboardInput {
  current: AtlasCurrent;
  universe: AtlasUniverse | null;
  q: string;
  rows: DashboardRow[];
  matched: number;
  total: number;
  shards: string[];
}

function statusCells(row: Record<SummaryStatus, number>): string {
  return STATUS_ORDER.map((s) => `<td class="num" data-status="${s}">${row[s] ?? 0}</td>`).join("");
}

function statusHeads(): string {
  return STATUS_ORDER.map((s) => `<th scope="col" class="num"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)}</th>`).join("");
}

export function atlasDashboardPage(input: DashboardInput): string {
  const { current, universe, q, rows, matched, total, shards } = input;
  const counts = current.counts;
  const libs = libraryViews(universe, []);
  const libraryRows = [...libs.values()].map((view) => {
    const c = counts.by_library[view.id] ?? { articles: 0, passages: 0, locations: 0, related: 0 };
    const tree = view.commit ? treeUrl(view.info, view.commit) : null;
    const commit = view.commit ? (tree ? `<a href="${attr(tree)}" rel="noopener"><code>@${text(sha7(view.commit))}</code></a>` : `<code>@${text(sha7(view.commit))}</code>`) : "";
    return `<tr><th scope="row">${text(view.info.name)}</th><td>${commit}</td><td class="num">${c.articles}</td><td class="num">${c.passages}</td><td class="num">${c.locations}</td><td class="num">${c.related}</td></tr>`;
  }).join("");
  const importance = Object.entries(counts.by_importance)
    .sort(([a], [b]) => {
      const ia = IMPORTANCE_ORDER.indexOf(a), ib = IMPORTANCE_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a < b ? -1 : a > b ? 1 : 0);
    })
    .map(([key, bucket]) => `<tr><th scope="row">${text(importanceLabel(key))}</th><td class="num">${bucket.articles}</td><td class="num">${bucket.passages}</td>${statusCells(bucket.by_status)}</tr>`)
    .join("");
  const hasImportance = Object.keys(counts.by_importance).some((key) => key !== "unknown");
  const list = rows.map((row) => `<tr data-title="${attr(row.title.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase())}"><th scope="row"><a href="${attr(row.href)}">${text(row.title)}</a></th><td class="num">${row.passages}</td>${statusCells(row as unknown as Record<SummaryStatus, number>)}<td>${text(importanceLabel(row.importance))}</td></tr>`).join("\n");
  const showing = q
    ? `${matched} of ${total} articles match “${text(q)}”${matched > rows.length ? `; showing the first ${rows.length}` : ""}.`
    : `${total} articles${total > rows.length ? `; showing the first ${rows.length} by title. Search to narrow` : ""}.`;
  const main = `<main class="wl-atlas" id="atlas-dashboard">
<h1 class="atlas-h1">Formalization atlas</h1>
<p class="atlas-lead">Which statements in WikiProject Mathematics articles are formalized in Lean, and where: each passage is located in a frozen set of Lean libraries at pinned commits, with a link to the exact source lines.</p>
<p class="atlas-meta">Campaign ${text(current.campaign)} · ${universe ? `<a href="/atlas/universe/${attr(current.universe.sha256)}">universe ${text(current.universe.id)}</a>` : `universe ${text(current.universe.id)}`}: ${current.universe.libraries} libraries, frozen ${text(current.universe.frozen)} · built ${text(current.built_at.slice(0, 10))}</p>
<section class="atlas-strip" aria-labelledby="atlas-h-totals">
<h2 class="atlas-h2" id="atlas-h-totals">Totals</h2>
<p class="atlas-strip-lead">${counts.articles} articles · ${counts.passages} passages · ${counts.locations} Lean locations${counts.related ? ` · ${counts.related} related declaration${counts.related === 1 ? "" : "s"}` : ""}</p>
${statusBar(counts.by_status, "passages")}
<div class="atlas-chips">${STATUS_ORDER.map((s) => `<span class="atlas-chip is-static" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)} <span class="atlas-n">${counts.by_status[s] ?? 0}</span></span>`).join("")}</div>
</section>
<section aria-labelledby="atlas-h-libs">
<h2 class="atlas-h2" id="atlas-h-libs">By library</h2>
<div class="atlas-table-wrap"><table class="atlas-table"><thead><tr><th scope="col">Library</th><th scope="col">Commit</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th><th scope="col" class="num">Locations</th><th scope="col" class="num">Related only</th></tr></thead><tbody>${libraryRows}</tbody></table></div>
<p class="atlas-muted">A passage counts toward every library that states it; “related only” declarations state something else and are not counted as locations.</p>
</section>
${hasImportance ? `<section aria-labelledby="atlas-h-imp">
<h2 class="atlas-h2" id="atlas-h-imp">By WikiProject importance</h2>
<div class="atlas-table-wrap"><table class="atlas-table"><thead><tr><th scope="col">Importance</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th>${statusHeads()}</tr></thead><tbody>${importance}</tbody></table></div>
</section>` : ""}
<section aria-labelledby="atlas-h-list">
<h2 class="atlas-h2" id="atlas-h-list">Articles</h2>
<form class="atlas-search" method="get" action="/atlas" role="search"><label for="atlas-q">Search covered articles</label> <input id="atlas-q" name="q" type="search" value="${attr(q)}" maxlength="200" autocomplete="off"> <button type="submit">Search</button></form>
<p class="atlas-count" id="atlas-count" role="status">${showing}</p>
<div class="atlas-table-wrap"><table class="atlas-table atlas-list" id="atlas-list" data-total="${total}" data-shards="${attr(shards.join(" "))}"><thead><tr><th scope="col">Article</th><th scope="col" class="num">Passages</th>${statusHeads()}<th scope="col">Importance</th></tr></thead><tbody>
${list}
</tbody></table></div>
</section>
<p class="atlas-data">Data: <a href="/api/atlas/current">current build (JSON)</a> · record set <code title="${attr(current.record_set.seal_sha256)}">${text(current.record_set.seal_sha256.slice(0, 12))}</code></p>
</main>`;
  return shell({
    title: "Formalization atlas",
    description: `Which WikiProject Mathematics statements are formalized in Lean, and where: ${counts.articles} articles, ${counts.passages} passages.`,
    canonical: "https://wikilean.jackmccarthy.org/atlas",
    main,
  });
}

// ---- the universe page -------------------------------------------------------

export function atlasUniversePage(universe: AtlasUniverse): string {
  const rows = universe.libraries.map((lib: UniverseLibrary) => {
    const info = libraryInfo(lib.repo, lib);
    const repo = httpsUrl(info.repo);
    const tree = treeUrl(info, lib.commit);
    return `<tr><th scope="row">${text(info.name)} <span class="atlas-muted">${text(lib.id)}</span></th>` +
      `<td>${repo ? `<a href="${attr(repo)}" rel="noopener">${text(info.repo.replace(/^https:\/\//, ""))}</a>` : text(info.repo)}</td>` +
      `<td>${tree ? `<a href="${attr(tree)}" rel="noopener"><code>${text(lib.commit)}</code></a>` : `<code>${text(lib.commit)}</code>`}</td>` +
      `<td>${text(lib.toolchain ?? "")}</td><td>${text(lib.licence ?? "")}</td></tr>`;
  }).join("");
  const excluded = universe.excluded.length
    ? `<h2 class="atlas-h2">Excluded</h2><ul class="atlas-excluded">${universe.excluded.map((item) => `<li><b>${text(item.id)}</b>: ${text(item.reason)}</li>`).join("")}</ul>`
    : "";
  const main = `<main class="wl-atlas" id="atlas-universe">
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>Universe ${text(universe.id)}</span></nav>
<h1 class="atlas-h1">Universe ${text(universe.id)}</h1>
<p class="atlas-meta">${universe.libraries.length} libraries, frozen ${text(universe.frozen)} · sha256 <code>${text(universe.sha256)}</code></p>
<p>A “not found” in the atlas means exactly this: the passage was searched for in these libraries at these commits and nothing stating it was found. It says nothing about other libraries or later commits.</p>
<div class="atlas-table-wrap"><table class="atlas-table"><thead><tr><th scope="col">Library</th><th scope="col">Repository</th><th scope="col">Commit</th><th scope="col">Toolchain</th><th scope="col">Licence</th></tr></thead><tbody>${rows}</tbody></table></div>
${excluded}
<p class="atlas-data">Data: <a href="/api/atlas/universe/${attr(universe.sha256)}">universe (JSON)</a></p>
</main>`;
  return shell({ title: `Atlas universe ${universe.id}`, description: `The Lean libraries and commits of atlas universe ${universe.id}.`, main });
}

// ---- the article page's summary box ------------------------------------------

export function atlasSummaryBox(entry: IndexEntry, href: string): string {
  const counts = entry as unknown as Record<SummaryStatus, number>;
  const parts = STATUS_ORDER.filter((s) => counts[s] > 0)
    .map((s) => `<span class="atlas-mini" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${counts[s]} ${text(STATUS[s].plural)}</span>`)
    .join(" ");
  return `<link rel="stylesheet" href="${ATLAS_CSS}">\n` +
    `<aside class="wl-atlas-box" aria-label="Formalization atlas">` +
    `<span class="wl-atlas-box-h">Formalization atlas</span> ` +
    `<span class="wl-atlas-box-counts">${entry.passages} passage${entry.passages === 1 ? "" : "s"} of Wikipedia revision ${entry.revision} checked against Lean libraries: ${parts || "none yet"}</span> ` +
    `<a class="wl-atlas-box-link" href="${attr(href)}">See where<span aria-hidden="true"> →</span></a>` +
    `</aside>\n`;
}
