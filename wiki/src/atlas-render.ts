// Server-rendered HTML for the formalization atlas (docs/ATLAS.md, "UI"):
// the /atlas dashboard, one article's coverage page, the universe page, the
// 503/404 message page, and the compact box injected into article pages.
//
// Every value from the store is data: text goes through htmlEscape, every
// number through int() (a store object is cast, not validated, so a string
// where a count belongs must never reach the HTML raw), every href is either
// built here from validated parts or parsed and required to be https, and no
// element carries an inline event handler. The only script on these pages
// besides the shared theme bootstrap and header search is /assets/atlas.js
// (site/assets/atlas.js), which writes the DOM through textContent and
// createElement only. Status is never colour alone: each status has an icon, a
// word and its own stroke pattern (atlas.css).

import { htmlEscape } from "./engine/html.js";
import { SEARCHBOX_CSS, SEARCHBOX_HTML, SEARCHBOX_SCRIPT } from "./engine/searchbox.js";
import type { AuthUser } from "./auth.js";
import type {
  AtlasArticle, AtlasCurrent, AtlasLocation, AtlasPassage, AtlasUniverse, IndexEntry, ProminenceCounts, SummaryStatus,
} from "./atlas.js";
import { libraryInfo, repoHref, sourceUrl, treeUrl, type LibraryInfo, type UniverseLibrary } from "./libraries.js";

// Asset versions: bump with any change to site/assets/atlas.{css,js}.
export const ATLAS_CSS = "/assets/atlas.css?v=4";
export const ATLAS_JS = "/assets/atlas.js?v=2";
const STYLE_CSS = "/assets/style.css?v=9";

const text = (s: string) => htmlEscape(String(s ?? ""), false);
const attr = (s: string) => htmlEscape(String(s ?? ""), true);

// A store number as a non-negative integer: anything else counts as 0.
function count(value: unknown): number {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : 0;
}
// The same, as the text that goes into the HTML.
const int = (value: unknown) => String(count(value));
// A store list: anything but an array is empty, so a corrupt object never
// throws (a 500) and never reaches the HTML through a "length".
function list<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}
// The universe's libraries that name a repository (the builder writes no other).
function universeLibraries(universe: AtlasUniverse | null): UniverseLibrary[] {
  return list(universe?.libraries).filter((lib) => lib !== null && typeof lib === "object" && typeof lib.repo === "string");
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

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
  // A definition with the library index's statement flag (proposal §2.4).
  prop_definition: { label: "Prop definition", phrase: "a Prop-valued definition: it states the claim without proving it", alert: false },
  statement_only: { label: "statement only", phrase: "statement only: the proof is not finished", alert: true },
  wanted: { label: "wanted", phrase: "wanted: stated as a goal, with no proof", alert: true },
  axiom_dependent: { label: "axiom-dependent", phrase: "depends on an axiom beyond the standard three", alert: true },
};
// The challenge verdicts (proposal §2.7; build_store.CHALLENGES). The last three were judged
// by no challenge and read as unchallenged: challenge_pending is a record a prominence
// overturn put under a challenge rule after the challenge ran, so one is owed.
const CHALLENGE: Record<string, string> = {
  survived: "a challenger checked it",
  downgraded: "a challenger downgraded it",
  not_required: "not challenged",
  sampled_out: "not challenged (sampled out)",
  challenge_pending: "not challenged yet (a challenge is pending)",
};
const IMPORTANCE_ORDER = ["top", "high", "mid", "low", "bottom", "na", "unknown"];

// Proposal §2.6's table per location, as scripts/atlas/build_store.py ports it
// from formal-companion's cflib.vocab.derive_status (docs/ATLAS.md, check 6),
// with the maintainer's decision D1 (2026-10-18): an exact or close match to
// data (a definition without the statement flag, such as CRT's RingEquiv)
// formalizes a theorem. A coverage claim is not a per-location row: the store
// carries its effect in the passage's status, basis and coverage.
const PART_RELATIONS = new Set(["hasPart", "narrowMatch"]);
const DATA_MATCH_RELATIONS = new Set(["exactMatch", "closeMatch"]);
const UNPROVED = new Set(["statement_only", "wanted", "axiom_dependent"]);
const THEOREM_KINDS = new Set(["theorem", "conjecture"]);
const DEFINITION_KINDS = new Set(["definition", "construction", "notation"]);
const EITHER_KINDS = new Set(["example", "other"]);
type LocatedStatus = "formalized" | "statement_only" | "partial";
const LOCATED_RANK: Record<LocatedStatus, number> = { formalized: 0, statement_only: 1, partial: 2 };

function theoremRow(loc: AtlasLocation): LocatedStatus {
  if (PART_RELATIONS.has(loc.relation)) return "partial";
  if (loc.statement === true) return "statement_only";
  if (loc.proof_status === "proved") return "formalized";
  if (UNPROVED.has(loc.proof_status)) return "statement_only";
  // Data: D1 makes an exact or close match formalized; a broadMatch stays partial.
  return DATA_MATCH_RELATIONS.has(loc.relation) ? "formalized" : "partial";
}

function definitionRow(loc: AtlasLocation): LocatedStatus {
  if (PART_RELATIONS.has(loc.relation) || loc.relation === "broadMatch") return "partial";
  if (loc.proof_status === "definition") return "formalized";
  return UNPROVED.has(loc.proof_status) ? "statement_only" : "partial";
}

// The status one location gives a passage of `kind` (build_store.location_status);
// null for a relatedMatch, which never counts.
export function locationStatus(kind: string, loc: AtlasLocation): LocatedStatus | null {
  if (loc.relation === "relatedMatch") return null;
  const rows: LocatedStatus[] = [];
  if (THEOREM_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(theoremRow(loc));
  if (DEFINITION_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(definitionRow(loc));
  return rows.length ? rows.reduce((a, b) => (LOCATED_RANK[b] < LOCATED_RANK[a] ? b : a)) : null;
}

// A status outside the vocabulary (a corrupt store) renders as undecided
// rather than throwing; the builder never writes one.
function asStatus(value: string | null | undefined): SummaryStatus {
  return STATUS_ORDER.includes(value as SummaryStatus) ? (value as SummaryStatus) : "unresolved";
}

function proofOf(loc: Pick<AtlasLocation, "proof_status" | "statement">) {
  const status = loc.statement === true && loc.proof_status === "definition" ? "prop_definition" : loc.proof_status;
  return PROOF[status] ?? { label: String(status), phrase: String(status), alert: true };
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function importanceLabel(value: string | null | undefined): string {
  if (!value || value === "unknown") return "Not assessed";
  if (value === "na") return "N/A";
  return capital(value);
}

// SEAL's campaign token in words: "campaign <id>", or, for a run outside any
// campaign ("run:<run-id>[+<challenge-run-id>]"), the runs it came from.
export function campaignLabel(token: string): string {
  const value = String(token ?? "");
  const run = /^run:([^+]+)(?:\+(.+))?$/.exec(value);
  if (!run) return `campaign ${value}`;
  return `run ${run[1]}${run[2] ? ` with challenge run ${run[2]}` : ""}, outside any campaign`;
}

function sha7(commit: string): string {
  return String(commit ?? "").slice(0, 7);
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
  return `https://en.wikipedia.org/w/index.php?curid=${int(pageid)}&oldid=${int(revision)}`;
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

// The status word with its icon. The sentence that explains it is visible
// text beside it (passageBlock), so the pill carries no hover-only title.
function pill(status: SummaryStatus): string {
  const meta = STATUS[status];
  return `<span class="atlas-pill" data-status="${status}">` +
    `<span class="atlas-ico" aria-hidden="true">${meta.icon}</span>${text(meta.label)}</span>`;
}

// A small sample of a status's coverage-bar fill: the bar's legend.
function swatch(status: SummaryStatus): string {
  return `<span class="atlas-swatch" data-status="${status}" aria-hidden="true"></span>`;
}

// ---- quotes ----------------------------------------------------------------

// The export quotes at most a passage's first QUOTE_MAX_WORDS words: the
// verbatim-quotation cap of the record format (proposal §2.7 and §10;
// build_store.QUOTE_MAX_WORDS), so a longer passage's quote stops mid-sentence.
export const QUOTE_MAX_WORDS = 12;

// Whether the quote is the capped head of a longer passage rather than all of
// it. The store says so (build_store.quote_capped, which compares the quote
// with the record's span_sha256); a store built before it gets the word count,
// a quote at the cap read as a head.
export function quoteCapped(passage: Pick<AtlasPassage, "quote" | "quote_capped">): boolean {
  if (typeof passage.quote_capped === "boolean") return passage.quote_capped;
  const exact = String(passage.quote?.exact ?? "").trim();
  return exact !== "" && exact.split(/\s+/).length >= QUOTE_MAX_WORDS;
}

// The mark after a capped head: an ellipsis outside the quotation (after its
// highlight or its closing quotation mark, never inside it, so the quote stays
// verbatim), with words for a screen reader, which may not voice "…".
const QUOTE_CONTINUES = `<span class="atlas-affix" data-quote-cut=""><span aria-hidden="true">&#xa0;…</span>` +
  `<span class="atlas-sr"> (the passage continues)</span></span>`;

// ---- counts by prominence ----------------------------------------------------

// The article's own statements: its subject and central passages, counted
// apart from the supporting ones (worked examples, setup sentences), which
// otherwise set the headline (build_store._prominence_counts).
const CENTRAL_PROMINENCES = ["subject", "central"];

function emptyStatusCounts(): Record<SummaryStatus, number> {
  return Object.fromEntries(STATUS_ORDER.map((s) => [s, 0])) as Record<SummaryStatus, number>;
}

// The central statements' counts by status, from a summary's by_prominence;
// null when the store has none (built before it).
function centralFromSummary(raw: Record<string, ProminenceCounts> | null | undefined): Record<SummaryStatus, number> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const counts = emptyStatusCounts();
  for (const prominence of CENTRAL_PROMINENCES) {
    const bucket = raw[prominence];
    for (const s of STATUS_ORDER) counts[s] += count(bucket?.by_status?.[s]);
  }
  return counts;
}

// The same, from an article's passages; null when no passage carries a
// prominence (a store built before every record had one).
function centralFromPassages(passages: AtlasPassage[]): Record<SummaryStatus, number> | null {
  if (!passages.some((p) => typeof p.prominence === "string")) return null;
  const counts = emptyStatusCounts();
  for (const passage of passages) {
    if (CENTRAL_PROMINENCES.includes(passage.prominence as string)) counts[passageStatus(passage)] += 1;
  }
  return counts;
}

// "Central statements": the subject and central passages, counted by status,
// next to the bar over all passages.
function centralLine(counts: Record<SummaryStatus, number>, all: number, scope: string): string {
  const total = STATUS_ORDER.reduce((sum, s) => sum + counts[s], 0);
  const parts = STATUS_ORDER.filter((s) => counts[s] > 0)
    .map((s) => `<span class="atlas-mini" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(counts[s])} ${text(STATUS[s].plural)}</span>`)
    .join(", ");
  return `<p class="atlas-strip-lead" id="atlas-central"><b>Central statements</b>, ${text(scope)}: ` +
    (total ? `${int(total)} of ${plural(count(all), "passage", "passages")} — ${parts}.` : "none.") + `</p>`;
}

// ---- shell -----------------------------------------------------------------

function shell(opts: {
  title: string; description: string; canonical?: string; main: string; script?: boolean; dashboard?: boolean;
}): string {
  // aria-current="page" only on /atlas itself: an article or universe page is
  // inside the atlas but is not the page the nav link opens.
  const current = opts.dashboard ? ` aria-current="page"` : "";
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
      <a class="wl-navlink" href="/atlas"${current}>Atlas</a>
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
  <p>The mapping (which passage is formalized where) is released under <a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>, like WikiLean&#x27;s annotations. Links are found by an AI pipeline; some are re-checked by a second AI pass, and each location says whether it was. Signed-in readers can confirm or report a location.</p>
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
  for (const lib of universeLibraries(universe)) {
    views.set(lib.id, { id: lib.id, info: libraryInfo(lib.repo, lib), commit: lib.commit });
  }
  for (const loc of extra) {
    if (!views.has(loc.library)) views.set(loc.library, { id: loc.library, info: libraryInfo(loc.repo, null), commit: loc.commit });
  }
  return views;
}

// Libraries by the name a reader sees, then by universe id, so a tie is stable.
function byName(a: LibraryView, b: LibraryView): number {
  return a.info.name.localeCompare(b.info.name, "en") || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

// A table that may scroll sideways on a phone: a labelled, focusable region,
// so a keyboard user can scroll it (axe scrollable-region-focusable). Its name
// says "table" so it differs from the section around it.
function tableWrap(label: string, table: string): string {
  return `<div class="atlas-table-wrap" role="region" tabindex="0" aria-label="${attr(`Table: ${label}`)}">${table}</div>`;
}

function statusBar(raw: Record<SummaryStatus, number>, one: string, many: string): string {
  const counts = Object.fromEntries(STATUS_ORDER.map((s) => [s, count(raw?.[s])])) as Record<SummaryStatus, number>;
  const total = STATUS_ORDER.reduce((sum, s) => sum + counts[s], 0);
  if (total === 0) return "";
  const label = STATUS_ORDER.filter((s) => counts[s]).map((s) => `${counts[s]} ${STATUS[s].plural}`).join(", ");
  const segments = STATUS_ORDER.filter((s) => counts[s]).map((s) => {
    const pct = ((counts[s] / total) * 100).toFixed(2);
    return `<span class="atlas-seg" data-status="${s}" style="width:${pct}%" title="${attr(`${counts[s]} ${STATUS[s].plural}`)}"></span>`;
  }).join("");
  return `<div class="atlas-bar" role="img" aria-label="${attr(`${plural(total, one, many)}: ${label}`)}">${segments}</div>`;
}

// The location that justifies a located passage's status: the store's basis
// (build_store derive_status), or, for a store built before it existed, the
// highest-ranked location whose own status (locationStatus) is the passage's.
// Never simply the rank-1 location: rank 1 may state without a proof what
// rank 2 proves.
export function basisLocation(passage: AtlasPassage, status: SummaryStatus): AtlasLocation | null {
  const locations = list(passage.locations);
  if (passage.basis) {
    const hit = locations.find((loc) => loc.location_key === passage.basis);
    if (hit) return hit;
  }
  return locations.find((loc) => locationStatus(passage.kind, loc) === status) ?? null;
}

// Decision D1: the hasPart locations a survived coverage claim names, in the
// claim's order; [] when there is no claim, it has not survived, or a part is
// not one of the passage's hasPart locations (the builder refuses that).
function coverageParts(passage: AtlasPassage): AtlasLocation[] {
  const claim = passage.coverage;
  if (!claim || claim.challenge !== "survived") return [];
  const locations = list(passage.locations);
  const parts = list(claim.parts).map((key) => locations.find((loc) => loc.location_key === key));
  return parts.length >= 2 && parts.every((loc) => loc?.relation === "hasPart") ? (parts as AtlasLocation[]) : [];
}

// "Formalized by N parts together", with the parts listed: shown only for a
// survived claim. Its words follow what the claim gives on its own: formalized
// when every part is finished (proved, or data), statement only when one is
// not (build_store.coverage_status, the round-three reading of D1). A store
// built before that reading could leave a survived claim no status.
function coverageBlock(passage: AtlasPassage, parts: AtlasLocation[], libs: Map<string, LibraryView>): string {
  const n = parts.length;
  const status = passage.coverage?.status;
  const lead = status === "formalized" ? `Formalized by ${n} parts together`
    : status === "statement_only" ? `Stated by ${n} parts together, not all of them finished`
    : `Covered by ${n} parts together, not all of which count toward the status`;
  const items = parts.map((loc) => {
    const info = libs.get(loc.library)?.info ?? libraryInfo(loc.repo, null);
    return `<li><span class="atlas-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span> ` +
      `${declName(String(loc.declaration ?? ""))} ` +
      `<span class="atlas-muted">(${text(proofOf(loc).label)})</span></li>`;
  }).join("");
  return `<div class="atlas-coverage" data-coverage-status="${attr(status ?? "none")}">` +
    `<p class="atlas-why">${text(lead)}:</p><ol class="atlas-coverage-parts">${items}</ol></div>`;
}

function statusSentence(passage: AtlasPassage, status: SummaryStatus, libs: Map<string, LibraryView>): string {
  const meta = STATUS[status];
  const best = basisLocation(passage, status);
  if (!best) return `${meta.label}.`;
  const name = libs.get(best.library)?.info.name ?? best.library;
  return `${meta.label}: ${name} ${RELATION_PHRASE[best.relation] ?? best.relation} (${proofOf(best).phrase}).`;
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
  const proof = proofOf(loc);
  const short = sha7(loc.commit);
  const relation = RELATION_WORD[loc.relation] ?? loc.relation;
  // end_line is optional (an index may record only the first line): "line N".
  const line = count(loc.line), end = count(loc.end_line);
  const lines = end && end !== line ? `lines ${line}–${end}` : `line ${line}`;
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
<p class="atlas-decl-line">${declName(String(loc.declaration ?? ""))}</p>
<p class="atlas-module">${text(loc.module)} · ${text(lines)}</p>
${loc.note ? `<p class="atlas-note"><span class="atlas-k">Convention:</span> ${text(loc.note)}</p>\n` : ""}<div class="atlas-card-foot">
<span class="atlas-prov">AI pipeline · rank ${int(loc.rank)} · ${text(loc.confidence)} confidence · ${text(CHALLENGE[loc.challenge] ?? loc.challenge)}</span>
${feedback}
</div>
</article>`;
}

function passageStatus(passage: AtlasPassage): SummaryStatus {
  return passage.outcome === "unresolved" ? "unresolved" : asStatus(passage.status);
}

// The passage's anchor (#p-<hex>), from its item "In-text #<hex>"; "" if malformed.
function passageHex(passage: AtlasPassage): string {
  return /#([0-9a-f]{8,16})$/.exec(String(passage.item ?? ""))?.[1] ?? "";
}

// A passage whose prominence is "subject" states what the article is about.
function isSubject(passage: AtlasPassage): boolean {
  return passage.prominence === "subject";
}

// Between the pieces of a passage heading: read, never seen (the flex gap
// separates them on screen).
const HEADING_SEPARATOR = `<span class="atlas-sr"> · </span>`;

function passageBlock(passage: AtlasPassage, article: AtlasArticle, ctx: CardContext): string {
  const status = passageStatus(passage);
  const located = passage.outcome !== "unresolved" && passage.outcome !== "absent"
    && (status === "formalized" || status === "statement_only" || status === "partial");
  const strong = passage.locations.filter((loc) => loc.relation !== "relatedMatch");
  const related = passage.locations.filter((loc) => loc.relation === "relatedMatch");
  const libraries = [...new Set(strong.map((loc) => loc.library))];
  const hex = passageHex(passage);
  const subject = isSubject(passage);
  // A subject passage says so in its own tag, so its eyebrow is the kind alone.
  const eyebrow = [capital(passage.kind), subject ? "" : passage.prominence].filter(Boolean).join(" · ");
  const section = Array.isArray(passage.section) && passage.section.length ? passage.section.join(" › ") : "";
  const context = inContextUrl(passage);
  const quote = passage.quote;
  const parts: string[] = [];
  const prominence = typeof passage.prominence === "string" ? ` data-prominence="${attr(passage.prominence)}"` : "";
  // The producer's item id stays in the markup (data-item), not in the text.
  parts.push(`<li class="atlas-passage${subject ? " is-subject" : ""}" id="p-${hex}" data-status="${status}"${prominence} data-libraries="${attr(libraries.join(" "))}" data-item="${attr(passage.item)}">`);
  // The heading's pieces are flex items with a visual gap only, and the section's
  // "§ " is CSS generated content, so a visually hidden " · " between them keeps
  // its accessible name from running together ("Theorem · central · § Statement").
  const heading = [
    `<span class="atlas-eyebrow">${text(eyebrow)}</span>`,
    subject ? `<span class="atlas-subject">Subject of the article</span>` : "",
    section ? `<span class="atlas-section">${text(section)}</span>` : "",
  ].filter(Boolean).join(HEADING_SEPARATOR);
  parts.push(`<div class="atlas-p-head"><h3 class="atlas-p-h">${heading}</h3>${pill(status)}</div>`);
  // A located passage says, in words, which location gives it its status. An
  // absence and an undecided passage say so in their own boxes below. When a
  // survived coverage claim gives it its status (D1), the claim's line, with
  // the parts listed, is that sentence; otherwise the line follows it.
  const covering = located ? coverageParts(passage) : [];
  const byClaim = covering.length > 0 && passage.coverage?.basis === true && passage.coverage.status === status;
  if (located && !byClaim) parts.push(`<p class="atlas-why">${text(statusSentence(passage, status, ctx.libs))}</p>`);
  if (covering.length) parts.push(coverageBlock(passage, covering, ctx.libs));
  // The quote verbatim. A capped head (the passage goes on past the export's
  // 12-word cap) ends in an ellipsis outside the highlight, after any suffix;
  // otherwise a suffix that stops mid-sentence trails off as before.
  const capped = quoteCapped(passage);
  parts.push(`<blockquote class="atlas-quote" data-quote="${capped ? "head" : "whole"}">` +
    (quote.prefix ? `<span class="atlas-affix">…${text(quote.prefix)}</span>` : "") +
    `<mark>${text(quote.exact)}</mark>` +
    (quote.suffix ? `<span class="atlas-affix">${text(quote.suffix)}${capped || /[.!?:;]\s*$/.test(quote.suffix) ? "" : "…"}</span>` : "") +
    (capped ? QUOTE_CONTINUES : "") +
    `</blockquote>`);
  if (context) parts.push(`<p class="atlas-context"><a href="${attr(context)}" rel="noopener" target="_blank">Read it in context on Wikipedia<span aria-hidden="true"> ↗</span></a></p>`);
  if (strong.length) {
    parts.push(`<div class="atlas-cards">${strong.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
  if (passage.absence) {
    const a = passage.absence;
    parts.push(`<p class="atlas-absence"><span class="atlas-ico" aria-hidden="true">✕</span>` +
      `Searched ${int(a.libraries)} libraries in universe <a href="/atlas/universe/${attr(a.universe_sha256)}">${text(a.universe)}</a> ` +
      `(${text(article.universe.frozen)}) — not found. ` +
      `<span class="atlas-muted">The search receipt lists ${int(a.receipt?.queries)} queries and ${int(a.receipt?.candidates)} candidates examined; ${text(CHALLENGE[a.challenge] ?? a.challenge)}.</span></p>`);
  }
  if (related.length) {
    parts.push(`<h4 class="atlas-related-h">Related, but states something else</h4>`);
    parts.push(`<div class="atlas-cards is-related">${related.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
  if (passage.outcome === "unresolved") {
    parts.push(`<p class="atlas-undecided"><span class="atlas-ico" aria-hidden="true">?</span>Undecided — the pipeline could not settle whether this passage is formalized. It is not counted as not formalized.</p>`);
  }
  parts.push(`</li>`);
  return parts.join("\n");
}

function chip(filter: string, label: string, n: number, title: string, status?: SummaryStatus, icon?: string): string {
  const mark = (status ? swatch(status) : "") + (icon ? `<span class="atlas-ico" aria-hidden="true">${icon}</span>` : "");
  // A filter that would show nothing is not offered: a zero count is inert.
  if (count(n) === 0) {
    return `<span class="atlas-chip is-static is-zero"${status ? ` data-status="${status}"` : ""}>` +
      `${mark}${text(label)} <span class="atlas-n">0</span></span>`;
  }
  return `<button type="button" class="atlas-chip" data-filter="${attr(filter)}"${status ? ` data-status="${status}"` : ""} aria-pressed="false" title="${attr(title)}">` +
    `${mark}${text(label)} <span class="atlas-n">${int(n)}</span></button>`;
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
  const n = count(summary.passages);
  const nLocations = count(summary.locations), nRelated = count(summary.related);
  const assessment = article.assessment;
  const assessed = assessment && (assessment.class || assessment.importance)
    ? `WikiProject Mathematics: ${[assessment.class ? `${assessment.class}-class` : "", assessment.importance ? `${importanceLabel(assessment.importance)} importance` : ""].filter(Boolean).join(", ")}`
    : "";
  const statusChips = STATUS_ORDER.map((s) => chip(`status:${s}`, STATUS[s].label, summary.by_status?.[s] ?? 0,
    `Show only ${STATUS[s].plural} passages`, s, STATUS[s].icon)).join("");
  const libraryChips = [...libs.values()]
    .filter((view) => summary.by_library?.[view.id])
    .map((view) => {
      const counts = summary.by_library[view.id];
      const commit = view.commit ? ` at commit ${sha7(view.commit)}` : "";
      const passages = count(counts.passages), locations = count(counts.locations), related = count(counts.related);
      if (!passages) {
        return `<span class="atlas-chip is-static" title="${attr(`${view.info.name}${commit}: ${plural(related, "related declaration", "related declarations")} only`)}">${text(view.info.short)} <span class="atlas-n">related only</span></span>`;
      }
      return chip(`library:${view.id}`, view.info.short, passages,
        `${view.info.name}${commit}: ${plural(passages, "passage", "passages")}, ${plural(locations, "location", "locations")}. Show only those passages.`);
    }).join("");
  const nLibraries = int(article.universe.libraries);
  const nExcluded = universe ? list(universe.excluded).length : 0;
  const searched = universe
    ? `Searched <a href="/atlas/universe/${attr(article.universe.sha256)}">universe ${text(article.universe.id)}</a>: ${nLibraries} libraries, frozen ${text(article.universe.frozen)}${nExcluded ? `, ${int(nExcluded)} excluded` : ""}`
    : `Universe ${text(article.universe.id)} (${nLibraries} libraries, frozen ${text(article.universe.frozen)})`;
  // The article's subject passages, named in the strip with a link to each.
  // A capped head's ellipsis follows the closing quotation mark, outside the quote.
  const subjects = list(article.passages).filter(isSubject).map((passage) => {
    const hex = passageHex(passage);
    const label = text(`${capital(String(passage.kind ?? ""))}: “${String(passage.quote?.exact ?? "")}”`) +
      (quoteCapped(passage) ? QUOTE_CONTINUES : "");
    return `<li>${hex ? `<a href="#p-${hex}">${label}</a>` : label} ${pill(passageStatus(passage))}</li>`;
  });
  const subjectRow = subjects.length
    ? `<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-subject">Subject</span><ul class="atlas-subject-list" aria-labelledby="atlas-h-subject">${subjects.join("")}</ul></div>\n`
    : "";
  const central = centralFromPassages(list(article.passages));
  const centralRow = central ? `${centralLine(central, n, "the article's subject and central passages")}\n` : "";
  const anyCapped = list(article.passages).some((passage) => quoteCapped(passage));
  const main = `<main class="wl-atlas" id="atlas-article" data-pageid="${int(article.pageid)}" data-campaign="${attr(article.campaign)}">
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="atlas-h1">${text(article.title)}</h1>
<p class="atlas-meta">Wikipedia revision <a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}" rel="noopener">${int(article.revision)}</a>${assessed ? ` · ${text(assessed)}` : ""}${wikileanSlug ? ` · <a href="/${attr(encodeURIComponent(wikileanSlug))}">Annotated article on WikiLean</a>` : ""}</p>
<p class="atlas-meta">${searched} · ${text(campaignLabel(article.campaign))}</p>
<section class="atlas-strip" aria-labelledby="atlas-h-coverage">
<h2 class="atlas-h2" id="atlas-h-coverage">Coverage</h2>
<p class="atlas-strip-lead">${plural(n, "passage", "passages")} checked${nLocations ? `, ${plural(nLocations, "Lean location", "Lean locations")}` : ""}${nRelated ? ` (and ${plural(nRelated, "related declaration", "related declarations")})` : ""}.</p>
${statusBar(summary.by_status, "passage", "passages")}
${centralRow}${subjectRow}<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-status">Status</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-status">${statusChips}</div></div>
${libraryChips ? `<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-where">Where</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-where">${libraryChips}</div></div>` : ""}
<p class="atlas-filter-note" role="status"><span class="atlas-filter-text"></span> <button type="button" class="atlas-clear" hidden>Show all passages</button></p>
</section>
<h2 class="atlas-h2" id="atlas-h-passages">Passages</h2>
${anyCapped ? `<p class="atlas-muted" id="atlas-quote-note">Each quote is at most the first ${QUOTE_MAX_WORDS} words of its passage. An ellipsis after a quote, outside it, means the passage goes on; read it in context on Wikipedia.</p>\n` : ""}${article.passages.length ? `<ol class="atlas-passages" aria-labelledby="atlas-h-passages">\n${article.passages.map((p) => passageBlock(p, article, ctx)).join("\n")}\n</ol>` : `<p>No formal passages were found in this revision.</p>`}
<p class="atlas-data">Data: <a href="/api/atlas/article/${int(article.pageid)}">view model (JSON)</a> · <a href="/api/atlas/records/${int(article.pageid)}">Literature Links records (JSONL)</a> · record set <code title="${attr(article.record_set)}">${text(String(article.record_set ?? "").slice(0, 12))}</code></p>
${user ? `<p class="atlas-muted atlas-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they score the pipeline.</p>` : ""}
</main>`;
  const description = `Where ${article.title} is formalized in Lean: ${int(summary.by_status?.formalized)} of ${plural(n, "passage", "passages")} formalized.`;
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
  return STATUS_ORDER.map((s) => `<td class="num" data-status="${s}">${int(row?.[s])}</td>`).join("");
}

function statusHeads(): string {
  return STATUS_ORDER.map((s) => `<th scope="col" class="num"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)}</th>`).join("");
}

function libraryTags(ids: string[], libs: Map<string, LibraryView>): string {
  if (!ids.length) return `<span class="atlas-muted">none</span>`;
  return ids.map((id) => {
    const view = libs.get(id);
    return `<span class="atlas-libtag" title="${attr(view ? `${view.info.name} · ${view.info.repo}` : id)}">${text(view?.info.short ?? id)}</span>`;
  }).join(" ");
}

export function atlasDashboardPage(input: DashboardInput): string {
  const { current, universe, q, rows, matched, total, shards } = input;
  const counts = current.counts;
  const libs = libraryViews(universe, []);
  const commitOf = (view: LibraryView) => {
    const tree = view.commit ? treeUrl(view.info, view.commit) : null;
    const sha = view.commit ? `<code class="atlas-sha">@${text(sha7(view.commit))}</code>` : "";
    return view.commit ? (tree ? `<a href="${attr(tree)}" rel="noopener">${sha}</a>` : sha) : "";
  };
  // Libraries with a passage or a related declaration, most passages first, then
  // most complete locations, then by name; every other library of the universe
  // was searched and holds nothing in this build, so it is listed below the
  // table rather than as a row of zeros.
  const byLibrary = [...libs.values()].map((view) => ({
    view, c: counts.by_library?.[view.id] ?? { articles: 0, passages: 0, locations: 0, related: 0, complete: 0, incomplete: 0 },
  }));
  const held = byLibrary.filter(({ c }) => count(c.passages) > 0 || count(c.related) > 0)
    .sort((a, b) => count(b.c.passages) - count(a.c.passages) || count(b.c.complete) - count(a.c.complete)
      || byName(a.view, b.view));
  const empty = byLibrary.filter(({ c }) => !(count(c.passages) > 0 || count(c.related) > 0))
    .sort((a, b) => byName(a.view, b.view));
  const libraryRows = held.map(({ view, c }) => {
    const split = Number.isSafeInteger(c.complete) && Number.isSafeInteger(c.incomplete);
    return `<tr><th scope="row">${text(view.info.name)}</th><td>${commitOf(view)}</td><td class="num">${int(c.articles)}</td><td class="num">${int(c.passages)}</td>` +
      `<td class="num">${split ? int(c.complete) : "—"}</td><td class="num">${split ? int(c.incomplete) : "—"}</td><td class="num">${int(c.related)}</td></tr>`;
  }).join("");
  const libraryTable = held.length
    ? tableWrap("By library", `<table class="atlas-table"><thead><tr><th scope="col">Library</th><th scope="col">Commit</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th><th scope="col" class="num">Complete</th><th scope="col" class="num">Incomplete</th><th scope="col" class="num">Related only</th></tr></thead><tbody>${libraryRows}</tbody></table>`)
    : `<p>No library holds a location or a related declaration in this build.</p>`;
  const emptyLibraries = empty.length
    ? `\n<details id="atlas-libs-empty"><summary>${plural(empty.length, held.length ? "more library" : "library", held.length ? "more libraries" : "libraries")} searched, with no locations in this build</summary>` +
      `<ul>${empty.map(({ view }) => `<li>${text(view.info.name)} ${commitOf(view)}</li>`).join("")}</ul></details>`
    : "";
  const central = centralFromSummary(counts.by_prominence);
  const centralRow = central ? `\n${centralLine(central, count(counts.passages), "the subject and central passages of every article")}` : "";
  const importance = Object.entries(counts.by_importance ?? {})
    .sort(([a], [b]) => {
      const ia = IMPORTANCE_ORDER.indexOf(a), ib = IMPORTANCE_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a < b ? -1 : a > b ? 1 : 0);
    })
    .map(([key, bucket]) => `<tr><th scope="row">${text(importanceLabel(key))}</th><td class="num">${int(bucket?.articles)}</td><td class="num">${int(bucket?.passages)}</td>${statusCells(bucket?.by_status)}</tr>`)
    .join("");
  const hasImportance = Object.keys(counts.by_importance ?? {}).some((key) => key !== "unknown");
  const list = rows.map((row) => `<tr data-title="${attr(row.title.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase())}"><th scope="row"><a href="${attr(row.href)}">${text(row.title)}</a></th><td class="num">${int(row.passages)}</td>${statusCells(row as unknown as Record<SummaryStatus, number>)}<td>${libraryTags(row.libraries ?? [], libs)}</td><td>${text(importanceLabel(row.importance))}</td></tr>`).join("\n");
  const shown = rows.length;
  const showing = q
    ? `${matched} of ${total} articles match “${text(q)}”${matched > shown ? `; showing the first ${shown}` : ""}.`
    : `${total} articles${total > shown ? `; showing the first ${shown} by title. Search to narrow` : ""}.`;
  // id → short name, for rows atlas.js builds from the index shards.
  const libraryNames = JSON.stringify(Object.fromEntries([...libs.values()].map((view) => [view.id, view.info.short])));
  const nRelated = count(counts.related);
  const main = `<main class="wl-atlas" id="atlas-dashboard">
<h1 class="atlas-h1">Formalization atlas</h1>
<p class="atlas-lead">Which statements in WikiProject Mathematics articles are formalized in Lean, and where: each passage is located in a frozen set of Lean libraries at pinned commits, with a link to the exact source lines.</p>
<p class="atlas-meta">${text(capital(campaignLabel(current.campaign)))} · ${universe ? `<a href="/atlas/universe/${attr(current.universe.sha256)}">universe ${text(current.universe.id)}</a>` : `universe ${text(current.universe.id)}`}: ${int(current.universe.libraries)} libraries, frozen ${text(current.universe.frozen)} · built ${text(String(current.built_at ?? "").slice(0, 10))}</p>
<section class="atlas-strip" aria-labelledby="atlas-h-totals">
<h2 class="atlas-h2" id="atlas-h-totals">Totals</h2>
<p class="atlas-strip-lead">${plural(count(counts.articles), "article", "articles")} · ${plural(count(counts.passages), "passage", "passages")} · ${plural(count(counts.locations), "Lean location", "Lean locations")}${nRelated ? ` · ${plural(nRelated, "related declaration", "related declarations")}` : ""}</p>
${statusBar(counts.by_status, "passage", "passages")}
<div class="atlas-chips atlas-legend">${STATUS_ORDER.map((s) => `<span class="atlas-chip is-static" data-status="${s}">${swatch(s)}<span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)} <span class="atlas-n">${int(counts.by_status?.[s])}</span></span>`).join("")}</div>${centralRow}
</section>
<section aria-labelledby="atlas-h-libs">
<h2 class="atlas-h2" id="atlas-h-libs">By library</h2>
${libraryTable}${emptyLibraries}
<p class="atlas-muted">A passage counts toward every library that states it. Complete locations are proved declarations or definitions; incomplete ones are stated without a finished proof (statement only, wanted, or axiom-dependent). “Related only” declarations state something else and are not counted as locations.</p>
</section>
${hasImportance ? `<section aria-labelledby="atlas-h-imp">
<h2 class="atlas-h2" id="atlas-h-imp">By WikiProject importance</h2>
${tableWrap("By WikiProject importance", `<table class="atlas-table"><thead><tr><th scope="col">Importance</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th>${statusHeads()}</tr></thead><tbody>${importance}</tbody></table>`)}
</section>` : ""}
<section aria-labelledby="atlas-h-list">
<h2 class="atlas-h2" id="atlas-h-list">Articles</h2>
<form class="atlas-search" method="get" action="/atlas" role="search"><label for="atlas-q">Search covered articles</label> <input id="atlas-q" name="q" type="search" value="${attr(q)}" maxlength="200" autocomplete="off"> <button type="submit">Search</button></form>
<p class="atlas-count" id="atlas-count" role="status">${showing}</p>
${tableWrap("Articles", `<table class="atlas-table atlas-list" id="atlas-list" data-total="${int(total)}" data-shards="${attr(shards.join(" "))}" data-library-names="${attr(libraryNames)}"><thead><tr><th scope="col">Article</th><th scope="col" class="num">Passages</th>${statusHeads()}<th scope="col">Libraries</th><th scope="col">Importance</th></tr></thead><tbody>
${list}
</tbody></table>`)}
</section>
<p class="atlas-data">Data: <a href="/api/atlas/current">current build (JSON)</a> · record set <code title="${attr(current.record_set.seal_sha256)}">${text(String(current.record_set.seal_sha256 ?? "").slice(0, 12))}</code></p>
</main>`;
  return shell({
    title: "Formalization atlas",
    description: `Which WikiProject Mathematics statements are formalized in Lean, and where: ${plural(count(counts.articles), "article", "articles")}, ${plural(count(counts.passages), "passage", "passages")}.`,
    canonical: "https://wikilean.jackmccarthy.org/atlas",
    main,
    dashboard: true,
  });
}

// ---- the universe page -------------------------------------------------------

export function atlasUniversePage(universe: AtlasUniverse): string {
  const libraries = universeLibraries(universe);
  const excludedItems = list(universe.excluded).filter((item) => item !== null && typeof item === "object");
  const rows = libraries.map((lib: UniverseLibrary) => {
    const info = libraryInfo(lib.repo, lib);
    // GitHub repositories only (repoHref); any other host is named, not linked.
    const repo = repoHref(info.repo);
    const tree = treeUrl(info, lib.commit);
    return `<tr><th scope="row">${text(info.name)} <span class="atlas-muted">${text(lib.id)}</span></th>` +
      `<td>${repo ? `<a href="${attr(repo)}" rel="noopener">${text(info.repo.replace(/^https:\/\//, ""))}</a>` : text(info.repo)}</td>` +
      `<td>${tree ? `<a href="${attr(tree)}" rel="noopener"><code>${text(lib.commit)}</code></a>` : `<code>${text(lib.commit)}</code>`}</td>` +
      `<td>${text(lib.toolchain ?? "")}</td><td>${text(lib.licence ?? "")}</td></tr>`;
  }).join("");
  const excluded = excludedItems.length
    ? `<h2 class="atlas-h2">Excluded</h2><ul class="atlas-excluded">${excludedItems.map((item) => `<li><b>${text(item.id)}</b>: ${text(item.reason)}</li>`).join("")}</ul>`
    : "";
  const main = `<main class="wl-atlas" id="atlas-universe">
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>Universe ${text(universe.id)}</span></nav>
<h1 class="atlas-h1">Universe ${text(universe.id)}</h1>
<p class="atlas-meta">${int(libraries.length)} libraries, frozen ${text(universe.frozen)} · sha256 <code>${text(universe.sha256)}</code></p>
<p>A “not found” in the atlas means exactly this: the passage was searched for in these libraries at these commits and nothing stating it was found. It says nothing about other libraries or later commits.</p>
${tableWrap(`Libraries of universe ${universe.id}`, `<table class="atlas-table"><thead><tr><th scope="col">Library</th><th scope="col">Repository</th><th scope="col">Commit</th><th scope="col">Toolchain</th><th scope="col">Licence</th></tr></thead><tbody>${rows}</tbody></table>`)}
${excluded}
<p class="atlas-data">Data: <a href="/api/atlas/universe/${attr(universe.sha256)}">universe (JSON)</a></p>
</main>`;
  return shell({ title: `Atlas universe ${universe.id}`, description: `The Lean libraries and commits of atlas universe ${universe.id}.`, main });
}

// ---- the article page's summary box ------------------------------------------

// The box sits under WikiLean's own status badges, which count this page's
// annotations, so it says that its counts are the atlas's, for the Wikipedia
// revision the atlas checked, and names the page's revision when they differ.
export function atlasSummaryBox(entry: IndexEntry, href: string, pageRevision: number | null = null): string {
  const counts = entry as unknown as Record<SummaryStatus, number>;
  const parts = STATUS_ORDER.filter((s) => count(counts[s]) > 0)
    .map((s) => `<span class="atlas-mini" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(counts[s])} ${text(STATUS[s].plural)}</span>`)
    .join(" ");
  const revision = count(entry.revision);
  const other = pageRevision !== null && Number.isSafeInteger(pageRevision) && pageRevision !== revision
    ? ` <span class="wl-atlas-box-rev">This page shows revision ${int(pageRevision)}.</span>`
    : "";
  return `<link rel="stylesheet" href="${ATLAS_CSS}">\n` +
    `<aside class="wl-atlas-box" aria-label="Formalization atlas">` +
    `<span class="wl-atlas-box-h">Formalization atlas</span> ` +
    `<span class="wl-atlas-box-counts">Separately from this page&#x27;s annotations, the atlas checked ${plural(count(entry.passages), "passage", "passages")} of Wikipedia revision ${int(revision)} against Lean libraries: ${parts || "none yet"}.${other}</span> ` +
    `<a class="wl-atlas-box-link" href="${attr(href)}">See where<span aria-hidden="true"> →</span></a>` +
    `</aside>\n`;
}
