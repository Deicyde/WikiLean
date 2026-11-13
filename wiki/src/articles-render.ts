// Server-rendered HTML for the articles plane (docs/ARTICLES.md, "UI"):
// the /articles dashboard, one article's coverage page (the card list), the article
// reader (the pinned revision with its passages highlighted, when the store has it),
// the universe page, the 503/404 message page, and the compact box injected into
// article pages.
//
// Every value from the store is data: text goes through htmlEscape, every
// number through int() (a store object is cast, not validated, so a string
// where a count belongs must never reach the HTML raw), every href is either
// built here from validated parts or parsed and required to be https, and no
// element carries an inline event handler. The only scripts on these pages
// besides the shared theme bootstrap and header search are /assets/articles.js
// (site/assets/articles.js) and, on an article with a reader, /assets/articles-reader.js,
// which write the DOM through textContent, setAttribute and createElement only. The
// reader's revision is the one piece of HTML from the store, served as the builder
// checked it (build_store --reader-html) and only when its sha256 is the view's. Every
// articles page enforces security.ts articlesCsp, which lets the two inline scripts run by
// their sha256 and no other inline script or handler: change either script and its
// hash there. Status is never colour alone: each status has an icon, a word and its
// own stroke pattern (articles.css).
//
// Provenance follows the maintainer's display rule (2026-10-19): every location
// card leads with the Lean code (the declaration, and its signature when the
// store has one), the passage's own text is central (in full when the store has
// it verified, else the record's quote), provenance is specific (the runs, the
// models and the operator that ran them and when, the record set, the export
// date and commit, as the store records them, and never a generic "AI" label)
// and sits behind a closed Details fold, and no voting data is shown. What the
// store does not record (a store built without the optional side files has no
// model, operator, signature or full text) is said to be unrecorded, or left
// out, never filled in. The data's audit status is said once per page, in one
// line under the header (ARTICLES_NOTICE), never as a badge on a card; what an audit
// measured, or that nothing has measured it, is in the page's "About" fold.
//
// The article first (the reader-polish pass, 2026-11-11). An article page opens with its
// title, ONE line of counts (countsLine: "3 passages · 1 formalized · 1 statement only ·
// 1 not formalized") and the status legend, which is also the filter (statusKey). Every
// other figure — the pinned revision, the WikiProject assessment, the libraries searched,
// the article's own statements, what an audit measured, the runs, how the text is shown —
// is in ONE closed "About this page" fold (aboutFold) under it. The words a reader meets
// outside that fold are plain: "libraries searched", "pinned revision", never "universe",
// "frozen", "central", "campaign" or "coverage claim".

import { siteNav, SITE_TOOLS } from "./site-nav.js";
import { htmlEscape } from "./engine/html.js";
import { passageTextHtml, quoteFragment, quoteSegments, renderQuote } from "./articles-math.js";
import { SEARCHBOX_CSS, SEARCHBOX_HTML, SEARCHBOX_SCRIPT } from "./engine/searchbox.js";
import type { AuthUser } from "./auth.js";
import type {
  ArticlesArticle, ArticlesAudit, ArticlesCurrent, ArticlesLocation, ArticlesPassage, ArticlesRun, ArticlesSupplements, ArticlesUniverse,
  AuditFigure, IndexEntry, PassageContext, ProminenceCounts, RecheckTallies, SummaryStatus,
} from "./articles.js";
import {
  libraryInfo, libraryInfos, repoHref, sourceUrl, treeUrl, type LibraryEntry, type LibraryInfo, type UniverseLibrary,
} from "./libraries.js";
import type { MergedArticle, MergedBundle, MergedLocation } from "./articles-overlay.js";

// Asset versions: bump with any change to site/assets/articles.{css,js}.
export const ARTICLES_CSS = "/assets/articles.css?v=13";
export const ARTICLES_JS = "/assets/articles.js?v=8";
// The article reader (articlesReaderPage) and the view switch on its card list: bump with any
// change to site/assets/articles-reader.{css,js}.
export const ARTICLES_READER_CSS = "/assets/articles-reader.css?v=6";
export const ARTICLES_READER_JS = "/assets/articles-reader.js?v=5";
const STYLE_CSS = "/assets/style.css?v=11";

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
function universeLibraries(universe: ArticlesUniverse | null): UniverseLibrary[] {
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
  // Never shown: a passage with no status is set aside (ARTICLE_STATUSES, below), so it gets no
  // status word, icon or count anywhere; this entry only completes the record.
  unresolved: { label: "No status yet", icon: "", plural: "with no status yet" },
};
const STATUS_ORDER: SummaryStatus[] = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"];
// The statuses the pages show. A passage with no status ("unresolved" in the store) gets no
// highlight and no status until someone reads it (the maintainer's decision, 2026-11-02, after
// PD5): the article views set it aside (articleParts), and the dashboard and the article box leave
// it out of their counts and their statuses, and do not count it either. The store's JSON keeps it.
const ARTICLE_STATUSES: SummaryStatus[] = STATUS_ORDER.filter((s) => s !== "unresolved");

// A store's counts by status as the pages show them: the statuses a passage can be given, and
// none for the passages that have none yet.
function shownStatusCounts(raw: Partial<Record<SummaryStatus, number>> | null | undefined): Record<SummaryStatus, number> {
  const counts = { formalized: 0, statement_only: 0, partial: 0, not_formalized: 0, unresolved: 0 };
  for (const s of ARTICLE_STATUSES) counts[s] = count(raw?.[s]);
  return counts;
}


// What each status means, in a reader's words: the legend's second column, the
// title of each filter button, and the sentence the reader's highlight legend gives.
const READER_LEGEND: Record<SummaryStatus, string> = {
  formalized: "a Lean declaration states it, proved, or a Lean definition defines it",
  statement_only: "Lean states it, but the proof is not finished",
  partial: "Lean states part of it, or a more general or special form",
  not_formalized: "searched every library and not found",
  unresolved: "nobody has read it yet; not counted as not formalized",
};

const RELATION_WORD: Record<string, string> = {
  exactMatch: "exact match",
  closeMatch: "close match",
  broadMatch: "more general",
  narrowMatch: "special case",
  hasPart: "states part",
  relatedMatch: "related",
};
// What a card's note explains, by its relation: the label in front of it.
const NOTE_LABEL: Record<string, string> = {
  exactMatch: "Note",
  closeMatch: "Why close, not exact",
  broadMatch: "Why more general",
  narrowMatch: "Why a special case",
  hasPart: "Why only part",
  relatedMatch: "How it differs",
};
// The page's legend of the relation words, in the record's order of strength.
const RELATION_ORDER = ["exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch"];
// A coverage bundle's own relation is one of the strong three (its weakest
// part's), in the same order (articles-overlay BUNDLE_RELATIONS).
const BUNDLE_RELATION_ORDER = ["exactMatch", "closeMatch", "broadMatch"];
const RELATION_LEGEND: Record<string, string> = {
  exactMatch: "states the passage exactly",
  closeMatch: "states it up to a convention or a difference the note names",
  broadMatch: "states a more general result, which implies it",
  narrowMatch: "states a special case of it",
  hasPart: "states one part of it",
  relatedMatch: "states something related but different; never counted",
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
// The challenge verdicts (proposal §2.7; build_store.CHALLENGES), as the sentence
// a Details fold says, naming the challenge run when the campaign token names
// one. The last three were judged by no challenge and read as unchallenged:
// challenge_pending is a record a prominence overturn put under a challenge rule
// after the challenge ran, so one is owed. A not_required record of a set whose
// token names no challenge run gives no reason: a set sealed with
// `tally.py --unchallenged` had no challenge to exempt it.
export function challengeSentence(challenge: string, runs: CampaignRuns | null): string {
  const run = runs?.challenge ? ` <code>${text(runs.challenge)}</code>` : "";
  switch (challenge) {
    case "survived": return `Re-checked in challenge run${run || " (unnamed)"}: it survived.`;
    case "downgraded": return `Re-checked in challenge run${run || " (unnamed)"}, which downgraded it.`;
    case "not_required": return run ? `Not re-checked: challenge run${run} did not require it.` : "Not re-checked.";
    case "sampled_out": return `Not re-checked: sampled out${run ? ` of challenge run${run}` : " of the challenge"}.`;
    case "challenge_pending": return "Not re-checked yet: a challenge is owed (the passage's prominence changed after the challenge ran).";
    default: return `Challenge: ${text(challenge)}.`;
  }
}
const IMPORTANCE_ORDER = ["top", "high", "mid", "low", "bottom", "na", "unknown"];

// Proposal §2.6's table per location, as scripts/articles/build_store.py ports it
// from formal-companion's cflib.vocab.derive_status (docs/ARTICLES.md, check 6),
// with the maintainer's decision D1 (2026-10-18): an exact or close match to
// data (a definition without the statement flag, such as CRT's RingEquiv)
// formalizes a theorem. Under status rule 3 (R-BROAD; a /2 store's seal names
// it) a broadMatch to data formalizes a theorem and a broadMatch to a definition
// formalizes a definition. A coverage claim is not a per-location row: the store
// carries its effect in the passage's status, basis and coverage. The page reads
// the store's basis first, so these rows are only the fallback for a store
// without one.
const PART_RELATIONS = new Set(["hasPart", "narrowMatch"]);
const DATA_MATCH_RELATIONS = new Set(["exactMatch", "closeMatch"]);
const UNPROVED = new Set(["statement_only", "wanted", "axiom_dependent"]);
const THEOREM_KINDS = new Set(["theorem", "conjecture"]);
const DEFINITION_KINDS = new Set(["definition", "construction", "notation"]);
const EITHER_KINDS = new Set(["example", "other"]);
type LocatedStatus = "formalized" | "statement_only" | "partial";
const LOCATED_RANK: Record<LocatedStatus, number> = { formalized: 0, statement_only: 1, partial: 2 };

const STATUS_RULE_BROAD = 3;
// Status rule 2 makes a survived bundle one more location of its own relation;
// under rule 1 it is one more exactMatch (build_store STATUS_RULE_BUNDLE,
// COVERAGE_ACTS_AS). Every part of a bundle is a hasPart location, and there
// are at least two of them (COVERAGE_RELATION, MIN_COVERAGE_PARTS).
const STATUS_RULE_BUNDLE = 2;
export const COVERAGE_ACTS_AS = "exactMatch";
export const BUNDLE_PART_RELATION = "hasPart";
export const MIN_BUNDLE_PARTS = 2;
export const MAX_BUNDLE_PARTS = 5;

function theoremRow(loc: ArticlesLocation, statusRule: number): LocatedStatus {
  if (PART_RELATIONS.has(loc.relation)) return "partial";
  if (loc.statement === true) return "statement_only";
  if (loc.proof_status === "proved") return "formalized";
  if (UNPROVED.has(loc.proof_status)) return "statement_only";
  // Data: D1 makes an exact or close match formalized; a broadMatch stays
  // partial under status rules 1 and 2 and formalizes under 3 (R-BROAD).
  return DATA_MATCH_RELATIONS.has(loc.relation) || statusRule >= STATUS_RULE_BROAD ? "formalized" : "partial";
}

function definitionRow(loc: ArticlesLocation, statusRule: number): LocatedStatus {
  if (PART_RELATIONS.has(loc.relation)) return "partial";
  if (loc.relation === "broadMatch") {
    return statusRule >= STATUS_RULE_BROAD && loc.proof_status === "definition" ? "formalized" : "partial";
  }
  if (loc.proof_status === "definition") return "formalized";
  return UNPROVED.has(loc.proof_status) ? "statement_only" : "partial";
}

// The status one location gives a passage of `kind` (build_store.location_status)
// under the store's status rule (1 when the store names none); null for a
// relatedMatch, which never counts.
export function locationStatus(kind: string, loc: ArticlesLocation, statusRule = 1): LocatedStatus | null {
  if (loc.relation === "relatedMatch") return null;
  const rows: LocatedStatus[] = [];
  if (THEOREM_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(theoremRow(loc, statusRule));
  if (DEFINITION_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(definitionRow(loc, statusRule));
  return rows.length ? rows.reduce((a, b) => (LOCATED_RANK[b] < LOCATED_RANK[a] ? b : a)) : null;
}

// ---- the coverage stand-in (decision D1, round three) ------------------------
//
// A survived coverage claim counts, for the derived status, as ONE more
// location: a fixed stand-in rather than any one of its parts. The builder's
// own port of it is scripts/articles/build_store.py part_finished /
// coverage_location / coverage_status, and this is the Worker's, pinned against
// the shared parity tables (test/articles.test.ts, "derived status rows").
//
// The store's own claims are not recomputed here — the builder stamped their
// status into the view model and the overlay reads it (articles-overlay.ts
// coverageStatus). This port is what a COMMUNITY bundle is judged by (issue
// #173), where there is no precomputed status and the parts may be a mix of
// sealed locations and community rows.

// A part is finished when it is proved, or data (a definition without the
// library index's statement flag). Unfinished: statement_only, wanted or
// axiom_dependent, or a Prop-valued definition (the flag set) whatever its
// proof status — it states a clause and proves none.
export function partFinished(loc: Pick<ArticlesLocation, "proof_status" | "statement">): boolean {
  return loc.proof_status === "proved" || (loc.proof_status === "definition" && loc.statement !== true);
}

// The one location a claim acts as, on the theorem or the definition row: a
// proved declaration (theorem row) or data (definition row) when every part is
// finished, a statement_only declaration on both rows otherwise.
function coverageLocation(row: "theorem" | "definition", finished: boolean, relation: string): ArticlesLocation {
  const proof = finished ? (row === "theorem" ? "proved" : "definition") : "statement_only";
  return { relation, proof_status: proof, statement: false } as ArticlesLocation;
}

// The status a coverage bundle gives a passage of `kind` on its own; null when
// it gives none (fewer than two parts, or a part that is not a hasPart
// location). Its relation is exactMatch under status rule 1 and the bundle's
// own under rules 2 and 3 (build_store.coverage_status, COVERAGE_ACTS_AS).
export function coverageStatus(
  kind: string,
  parts: Array<Pick<ArticlesLocation, "relation" | "proof_status" | "statement">>,
  bundleRelation: string,
  statusRule = 1,
): LocatedStatus | null {
  if (parts.length < MIN_BUNDLE_PARTS || !parts.every((part) => part.relation === BUNDLE_PART_RELATION)) return null;
  const finished = parts.every(partFinished);
  const relation = statusRule >= STATUS_RULE_BUNDLE ? bundleRelation : COVERAGE_ACTS_AS;
  const rows: LocatedStatus[] = [];
  if (THEOREM_KINDS.has(kind) || EITHER_KINDS.has(kind)) {
    rows.push(theoremRow(coverageLocation("theorem", finished, relation), statusRule));
  }
  if (DEFINITION_KINDS.has(kind) || EITHER_KINDS.has(kind)) {
    rows.push(definitionRow(coverageLocation("definition", finished, relation), statusRule));
  }
  return rows.length ? rows.reduce((a, b) => (LOCATED_RANK[b] < LOCATED_RANK[a] ? b : a)) : null;
}

// A status outside the vocabulary (a corrupt store) reads as no status, so the passage is set
// aside with the others that have none, rather than throwing; the builder never writes one.
function asStatus(value: string | null | undefined): SummaryStatus {
  return STATUS_ORDER.includes(value as SummaryStatus) ? (value as SummaryStatus) : "unresolved";
}

function proofOf(loc: Pick<ArticlesLocation, "proof_status" | "statement">) {
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

// The runs a campaign token names: the locate run and the challenge run of
// "run:<run-id>[+<challenge-run-id>]"; null for a campaign id, which names no run.
export interface CampaignRuns { locate: string; challenge: string | null }
export function campaignRuns(token: string): CampaignRuns | null {
  const run = /^run:([^+]+)(?:\+(.+))?$/.exec(String(token ?? ""));
  return run ? { locate: run[1], challenge: run[2] ?? null } : null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// An ISO 8601 UTC time's day and minute; null when it is not one.
function timeParts(iso: string): { y: string; m: string; d: number; hm: string } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso ?? ""));
  const month = m ? MONTHS[Number(m[2]) - 1] : undefined;
  return m && month ? { y: m[1], m: month, d: Number(m[3]), hm: `${m[4]}:${m[5]}` } : null;
}

// "1 Nov 2026" from an ISO time; "" when it is not one.
function dayOf(iso: string): string {
  const t = timeParts(iso);
  return t ? `${t.d} ${t.m} ${t.y}` : "";
}

// The days an ISO window spans, UTC: "1 Nov 2026", "30–31 Oct 2026",
// "31 Oct–1 Nov 2026" or "31 Dec 2026–1 Jan 2027"; "" when either is not a time.
function dayRange(start: string, end: string): string {
  const a = timeParts(start), b = timeParts(end);
  if (!a || !b) return "";
  if (a.y !== b.y) return `${a.d} ${a.m} ${a.y}–${b.d} ${b.m} ${b.y}`;
  if (a.m !== b.m) return `${a.d} ${a.m}–${b.d} ${b.m} ${a.y}`;
  return a.d === b.d ? `${a.d} ${a.m} ${a.y}` : `${a.d}–${b.d} ${a.m} ${a.y}`;
}

// The same window to the minute: "1 Nov 2026, 03:19–14:25 UTC" or
// "31 Oct 2026, 04:59 – 1 Nov 2026, 03:06 UTC".
function timeRange(start: string, end: string): string {
  const a = timeParts(start), b = timeParts(end);
  if (!a || !b) return "";
  return a.y === b.y && a.m === b.m && a.d === b.d
    ? `${a.d} ${a.m} ${a.y}, ${a.hm}–${b.hm} UTC`
    : `${a.d} ${a.m} ${a.y}, ${a.hm} – ${b.d} ${b.m} ${b.y}, ${b.hm} UTC`;
}

// The seal fields a /2 store carries per article (build_store.seal_summary).
interface SealSummary { set_id?: string; schema?: string; status_rule?: number; created?: string; skill_commit?: string }

// What the record set does not record, said rather than guessed (display rule).
const UNRECORDED = "The record set does not record the model or the operator that ran these runs.";

// One record set's provenance as Details lines: its runs, its set, when and by
// which producer commit it was exported, under which status rule, its seal.
// `named`: the runs are cited line by line after these lines (runLine), so the
// line naming them is left out rather than said twice.
function recordSetLines(campaign: string, seal: SealSummary | null | undefined, sealSha: string, named = false): string[] {
  const runs = campaignRuns(campaign);
  const lines: string[] = [];
  if (!(named && runs)) {
    lines.push(runs
      ? `Locate run <code>${text(runs.locate)}</code>${runs.challenge ? `, re-checked by challenge run <code>${text(runs.challenge)}</code>` : ", not re-checked by a challenge run"}.`
      : `Campaign <code>${text(campaign)}</code>.`);
  }
  if (seal && typeof seal === "object") {
    const day = dayOf(String(seal.created ?? ""));
    const tags = [typeof seal.schema === "string" ? text(seal.schema) : "",
      Number.isSafeInteger(seal.status_rule) ? `statuses by status rule ${int(seal.status_rule)}` : ""].filter(Boolean);
    lines.push(`Record set <code>${text(String(seal.set_id ?? ""))}</code>${tags.length ? ` (${tags.join(", ")})` : ""}` +
      `${day ? `, exported ${text(day)}` : ""} by formal-companion at commit <code title="${attr(String(seal.skill_commit ?? ""))}">${text(sha7(String(seal.skill_commit ?? "")))}</code>.`);
  }
  if (sealSha) lines.push(`Seal sha256 <code>${text(sealSha)}</code>.`);
  return lines;
}

// ---- provenance from the store's optional side file (build_store --provenance) --

// A run as the store cites it, or null when it is not one: a store object is
// cast, not validated, so a malformed run falls back to the run ids alone.
function asRun(value: unknown): ArticlesRun | null {
  if (!value || typeof value !== "object") return null;
  const run = value as unknown as Record<string, unknown>;
  const fields = ["run_id", "model", "model_name", "reviewer_model", "reviewer_model_name", "operator", "started", "harvested"];
  return fields.every((key) => typeof run[key] === "string" && run[key] !== "") ? (value as ArticlesRun) : null;
}

// The runs that made an article's records: its locate run and, when one
// re-checked them, its challenge run, and the inventory run that chose its
// passages when the store names one; null when the store names no models.
interface ArticleRuns { locate: ArticlesRun; challenge: ArticlesRun | null; inventory: ArticlesRun | null }
function articleRuns(article: ArticlesArticle): ArticleRuns | null {
  const prov = article.provenance;
  if (!prov || typeof prov !== "object") return null;
  const locate = asRun(prov.locate);
  return locate ? {
    locate, challenge: prov.challenge == null ? null : asRun(prov.challenge),
    inventory: prov.inventory == null ? null : asRun(prov.inventory),
  } : null;
}

// "Model X (model-x)": the model's name, then the id it ran as, which never breaks
// across lines at its hyphens.
function modelCite(name: string, id: string): string {
  return `${text(name)} <span class="articles-nowrap">(${text(id)})</span>`;
}

// "by Model X (model-x) on 31 Oct–1 Nov 2026 (UTC), locate run loc-… by
// operator": the run's model, its days and the login that ran it.
function runCite(run: ArticlesRun, kind: "locate" | "challenge"): string {
  const days = dayRange(run.started, run.harvested);
  return `by ${modelCite(run.model_name, run.model)}${days ? ` on ${text(days)} (UTC)` : ""}, ` +
    `${kind} run <code>${text(run.run_id)}</code> by ${text(run.operator)}`;
}

// challengeSentence's verdicts as a citation of the challenge run that gave
// them: who re-checked the record and when, or which run sampled it out.
// `held` is what a survived verdict says ("it survived", "upheld").
function challengeCitation(challenge: string, run: ArticlesRun | null, runs: CampaignRuns | null, held = "it survived"): string {
  if (!run) return challengeSentence(challenge, runs);
  const id = `<code>${text(run.run_id)}</code>`;
  const days = dayRange(run.started, run.harvested);
  switch (challenge) {
    case "survived": return `Re-checked ${runCite(run, "challenge")}: ${held}.`;
    case "downgraded": return `Re-checked ${runCite(run, "challenge")}, which downgraded it.`;
    case "not_required": return `Not re-checked: challenge run ${id} by ${text(run.operator)} did not require it.`;
    case "sampled_out":
      return `Not re-checked: sampled out of challenge run ${id} by ${text(run.operator)}, in which ` +
        `${modelCite(run.model_name, run.model)} re-checked a sample${days ? ` on ${text(days)} (UTC)` : ""}.`;
    default: return challengeSentence(challenge, runs);
  }
}

function rate(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? String(value) : "—";
}

// A [re-checked, of] pair from a store or a count: anything else is [0, 0].
function pair(value: unknown): [number, number] {
  return Array.isArray(value) && value.length === 2 && count(value[0]) <= count(value[1]) ? [count(value[0]), count(value[1])] : [0, 0];
}

// What a challenge re-checked on an article's passages: its Lean locations (related
// declarations aside) and its absences, central (subject or central) and supporting apart.
function recheckTallies(passages: ArticlesPassage[]): RecheckTallies {
  const t: RecheckTallies = { central: { locations: [0, 0], absences: [0, 0] }, supporting: { locations: [0, 0], absences: [0, 0] } };
  const rechecked = (verdict: unknown) => verdict === "survived" || verdict === "downgraded";
  for (const passage of passages) {
    const group = CENTRAL_PROMINENCES.includes(passage.prominence as string) ? t.central : t.supporting;
    for (const loc of list(passage.locations)) {
      if (loc.relation === "relatedMatch") continue;
      group.locations[1] += 1;
      if (rechecked(loc.challenge)) group.locations[0] += 1;
    }
    if (passage.absence) {
      group.absences[1] += 1;
      if (rechecked(passage.absence.challenge)) group.absences[0] += 1;
    }
  }
  return t;
}

// "3 of 4 Lean locations (75%)".
function share(n: number, of: number, noun: string): string {
  return `${int(n)} of ${int(of)} ${noun}${of ? ` (${Math.round((100 * n) / of)}%)` : ""}`;
}

// "5 locations and 2 absences", leaving out a kind there is none of.
function kinds(locations: number, absences: number): string {
  return [locations ? plural(locations, "location", "locations") : "", absences ? plural(absences, "absence", "absences") : ""]
    .filter(Boolean).join(" and ");
}

// The challenge's sampling, as what it re-checked: every record on a central or subject
// passage (when it did), a seeded sample of the supporting ones at the run's rates, and
// the totals, so the rates and the shares a reader sees agree. `scope` leads it
// ("In this article it", "Across the build it").
function recheckSentence(raw: RecheckTallies, sampling: ArticlesRun["sampling"], scope: string): string {
  const cl = pair(raw?.central?.locations), ca = pair(raw?.central?.absences);
  const sl = pair(raw?.supporting?.locations), sa = pair(raw?.supporting?.absences);
  const parts: string[] = [];
  if (cl[1] + ca[1] > 0) {
    const what = [cl[1] ? "Lean location" : "", ca[1] ? "absence" : ""].filter(Boolean).join(" and ");
    parts.push(cl[0] === cl[1] && ca[0] === ca[1]
      ? `every ${what} on a central or subject passage (${kinds(cl[1], ca[1])})`
      : [cl[1] ? `${int(cl[0])} of ${int(cl[1])} Lean locations` : "", ca[1] ? `${int(ca[0])} of ${int(ca[1])} absences` : ""]
        .filter(Boolean).join(" and ") + " on central or subject passages");
  }
  if (sl[1] + sa[1] > 0) {
    const s = sampling && typeof sampling === "object" && Number.isSafeInteger(sampling.seed)
      ? ` (seed ${int(sampling.seed)}; drawn at ${rate(sampling.strong_sample_rate)} for strong matches, ${rate(sampling.sample_rate)} for weaker links and ${rate(sampling.absence_sample_rate)} for absences)`
      : "";
    const drawn = [sl[1] ? `${int(sl[0])} of ${int(sl[1])} locations` : "", sa[1] ? `${int(sa[0])} of ${int(sa[1])} absences` : ""]
      .filter(Boolean).join(" and ");
    parts.push(`on supporting passages, a seeded sample${s}: ${drawn}`);
  }
  if (!parts.length) return "";
  const totals = [cl[1] + sl[1] ? share(cl[0] + sl[0], cl[1] + sl[1], "Lean locations") : "",
    ca[1] + sa[1] ? share(ca[0] + sa[0], ca[1] + sa[1], "absences") : ""].filter(Boolean).join(" and ");
  return ` ${scope} re-checked ${parts.join("; ")}. In all, ${totals} were re-checked.`;
}

// One run in an "About" fold: who ran it and when, to the minute, and the
// models of its two roles (the worker that wrote the records or re-checked
// them, and the reviewer of each change set before it was merged); a challenge
// run adds what it re-checked and how it sampled the rest, and an inventory run
// that it chose the passages.
function runLine(run: ArticlesRun, tallies: RecheckTallies | null = null, scope = "It"): string {
  const kind = run.mode === "locate-challenge" ? "Challenge" : run.mode === "locate-inventory" ? "Inventory" : "Locate";
  const window = timeRange(run.started, run.harvested);
  const s = run.sampling;
  let sampling = "";
  if (kind === "Challenge" && tallies) sampling = recheckSentence(tallies, s, scope);
  else if (s && typeof s === "object" && Number.isSafeInteger(s.seed)) {
    sampling = ` It sampled with seed ${int(s.seed)}, at ${rate(s.strong_sample_rate)} for strong matches, ${rate(s.sample_rate)} for weaker links and ${rate(s.absence_sample_rate)} for absences.`;
  }
  const chose = kind === "Inventory" ? " It listed the formal passages, each with its kind and prominence, that the locate run then searched." : "";
  return `${kind} run <code>${text(run.run_id)}</code>, run by ${text(run.operator)}${window ? `, ${text(window)}` : ""}: ` +
    `worker ${modelCite(run.model_name, run.model)}, reviewer ${modelCite(run.reviewer_model_name, run.reviewer_model)}.${sampling}${chose}`;
}

// What the store merged from its optional side files, as "About" lines: how
// much of the passages' text it shows in full, and where the signatures are from.
function supplementLines(supplements: ArticlesSupplements | null | undefined, withText: number | null, passages: number | null): string[] {
  if (!supplements || typeof supplements !== "object") return [];
  const lines: string[] = [];
  if (supplements.passage_text && typeof supplements.passage_text === "object") {
    const shown = withText === null ? "" : passages === null ? `${plural(withText, "passage", "passages")} ` : `${int(withText)} of ${plural(count(passages), "passage", "passages")} `;
    lines.push(`Passage text: ${shown}shown in full, each the slice of the pinned revision&#x27;s text that its record&#x27;s span sha256 hashes, checked when the store was built.`);
  }
  const sig = supplements.signatures;
  if (sig && typeof sig === "object") {
    const day = dayOf(String(sig.corpus_built ?? ""));
    const sha = String(sig.corpus_sha256 ?? "");
    lines.push(`Lean code: each declaration&#x27;s type, from the declaration corpus of universe ${text(String(sig.universe ?? ""))}` +
      `${day ? ` (built ${text(day)}, ` : " ("}sha256 <code title="${attr(sha)}">${text(sha.slice(0, 12))}</code>), at the commit its card links.`);
  }
  return lines;
}

// ---- what an audit of the links measured (build_store --audit-summary) ----------

// The data's audit status, one line under every articles page's header: a statement about the
// dataset, never a label on a card.
export const ARTICLES_NOTICE = "Preliminary: these links have not yet had an independent audit.";

// The notice as the pages place it, under the header.
const NOTICE_LINE = `<p class="articles-notice" id="articles-notice">${text(ARTICLES_NOTICE)}</p>`;

// What a store without an audit summary says in its "About" fold: a statement about the build,
// which may have been built without one whether or not an audit of its links exists.
export const UNMEASURED = "This build carries no accuracy figure from an audit of its links.";

// An audit as the store cites it, or null: a store object is cast, not validated, so a
// malformed one reads as no audit.
function asAudit(value: unknown): ArticlesAudit | null {
  if (!value || typeof value !== "object") return null;
  const audit = value as Record<string, unknown>;
  const strings = ["run_id", "operator", "started", "ended"].every((key) => typeof audit[key] === "string" && audit[key] !== "");
  return strings && Array.isArray(audit.models) && Array.isArray(audit.figures) && count(audit.items) > 0
    ? (value as ArticlesAudit) : null;
}

// A share as a whole percent, without its sign ("92"); null when it is not a share.
function percent(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? Math.round(100 * value) : null;
}

// One figure as the estimate it is: the share judged right and its 95% interval, from how many
// items drawn of how many it stands for, and, when the items were weighed, by what and the
// effective sample that leaves ("80% (95% interval 62–91%), from 16 drawn of 400, weighted by
// relation and library, an effective sample of 12.5"). Null when the store's figure is not one.
function auditFigure(f: AuditFigure): string | null {
  if (!f || typeof f !== "object" || typeof f.label !== "string") return null;
  const share = percent(f.share);
  const [low, high] = Array.isArray(f.ci95) && f.ci95.length === 2 ? [percent(f.ci95[0]), percent(f.ci95[1])] : [null, null];
  const drawn = count(f.drawn), population = count(f.population);
  if (share === null || low === null || high === null || low > share || share > high || drawn < 1 || population < drawn) return null;
  const weighed = typeof f.weights === "string" && f.weights !== "" && typeof f.n_eff === "number" && Number.isFinite(f.n_eff) && f.n_eff > 0
    ? `, weighted by ${text(f.weights)}, an effective sample of ${String(Math.round(10 * f.n_eff) / 10)}`
    : "";
  return `${text(f.label)}: ${share}% (95% interval ${low}–${high}%), from ${int(drawn)} drawn of ${int(population)}${weighed}`;
}

// How the audit stood to the runs it judged, as its summary says: blind (its judges saw neither
// each other's answers nor which run an item came from) and independent (its judges are not the
// runs' own kind of judge). Empty when the store says neither.
function auditStanding(audit: ArticlesAudit): string {
  if (typeof audit.blind !== "boolean" || typeof audit.independent !== "boolean") return "";
  return ` (${audit.blind ? (audit.independent ? "blind and independent" : "blind, not independent")
    : (audit.independent ? "independent, not blind" : "neither blind nor independent")})`;
}

// The "About" lines on accuracy: the figures an audit measured, each an estimate with its
// interval and what it rests on, and the run that measured them (its models, operator and window,
// the display rule's citation); or, without one, that the build carries no such figure.
function auditLines(raw: unknown): string[] {
  const audit = asAudit(raw);
  if (!audit) return [UNMEASURED];
  const figures = list(audit.figures).map(auditFigure).filter((line): line is string => line !== null);
  const models = list(audit.models)
    .filter((m) => m && typeof m === "object" && typeof m.model === "string" && typeof m.model_name === "string")
    .map((m) => modelCite(m.model_name, m.model));
  const window = timeRange(audit.started, audit.ended);
  return [
    `Accuracy, from an audit of a sample of ${plural(count(audit.items), "item", "items")}${auditStanding(audit)}: ${figures.length ? figures.join("; ") : "no figure recorded"}.`,
    `Audit run <code>${text(audit.run_id)}</code>, run by ${text(audit.operator)}${window ? `, ${text(window)}` : ""}` +
      `${models.length ? `: judged by ${models.join(", ")}` : ""}.`,
  ];
}

function detailsFold(cls: string, summary: string, lines: string[]): string {
  return `<details class="${cls}"><summary>${text(summary)}</summary><ul class="articles-prov-list">` +
    lines.map((line) => `<li>${line}</li>`).join("") + `</ul></details>`;
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
// plus a text fragment, so the browser scrolls to and highlights the quote. The
// fragment is built from the quote's prose only (articles-math quoteFragment): the
// rendered article shows neither the layer's MathML tokens nor its TeX. A quote
// with no specific run of prose links to the revision without one.
function inContextUrl(passage: ArticlesPassage): string | null {
  const base = httpsUrl(passage.subject_uri);
  if (!base || !base.startsWith("https://en.wikipedia.org/")) return null;
  const fragment = quoteFragment(String(passage.quote?.exact ?? ""), quoteCapped(passage) && !endsInSuffix(passage));
  return `${base.replace(/#.*$/, "")}${fragment ? `#:~:text=${fragment}` : ""}`;
}

// Namespace faint, leaf bold, with break opportunities after "." and "_" so a
// long Lean name wraps inside a narrow card.
function declName(name: string): string {
  const cut = name.lastIndexOf(".");
  const breakable = (s: string) => text(s).replace(/([._])/g, "$1<wbr>");
  const ns = cut > 0 ? `<span class="articles-ns">${breakable(name.slice(0, cut + 1))}</span>` : "";
  return `<code class="articles-decl">${ns}<b>${breakable(cut > 0 ? name.slice(cut + 1) : name)}</b></code>`;
}

// The status word with its icon. The sentence that explains it is visible
// text beside it (passageBlock), so the pill carries no hover-only title.
function pill(status: SummaryStatus): string {
  const meta = STATUS[status];
  return `<span class="articles-pill" data-status="${status}">` +
    `<span class="articles-ico" aria-hidden="true">${meta.icon}</span>${text(meta.label)}</span>`;
}

// A small sample of a status's coverage-bar fill: the bar's legend.
function swatch(status: SummaryStatus): string {
  return `<span class="articles-swatch" data-status="${status}" aria-hidden="true"></span>`;
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
export function quoteCapped(passage: Pick<ArticlesPassage, "quote" | "quote_capped">): boolean {
  if (typeof passage.quote_capped === "boolean") return passage.quote_capped;
  const exact = String(passage.quote?.exact ?? "").trim();
  return exact !== "" && exact.split(/\s+/).length >= QUOTE_MAX_WORDS;
}

// Whether the card, which shows the quote's suffix, shows where a capped
// head's passage ends: the store says the rest of the passage lies within that
// suffix (build_store.quote_extent). A store built before the flag reads a
// head's passage as going on past its suffix.
function endsInSuffix(passage: Pick<ArticlesPassage, "quote" | "passage_ends_in_suffix">): boolean {
  return passage.passage_ends_in_suffix === true && typeof passage.quote?.suffix === "string" && passage.quote.suffix !== "";
}

// The mark after a capped head: an ellipsis outside the quotation (after its
// highlight or its closing quotation mark, never inside it, so the quote stays
// verbatim), with words for a screen reader, which may not voice "…".
const QUOTE_CONTINUES = `<span class="articles-affix" data-quote-cut=""><span aria-hidden="true">&#xa0;…</span>` +
  `<span class="articles-sr"> (the passage continues)</span></span>`;

// ---- counts by prominence ----------------------------------------------------

// The article's own statements: its subject and central passages, counted
// apart from the supporting ones (worked examples, setup sentences), which
// otherwise set the headline (build_store._prominence_counts).
const CENTRAL_PROMINENCES = ["subject", "central"];

// The record's prominence in a reader's words, for the eyebrow over a passage. The
// store's own token stays in data-prominence, where the scripts read it.
const PROMINENCE_WORD: Record<string, string> = {
  subject: "the article's subject",
  central: "main statement",
  supporting: "supporting",
};

function emptyStatusCounts(): Record<SummaryStatus, number> {
  return Object.fromEntries(STATUS_ORDER.map((s) => [s, 0])) as Record<SummaryStatus, number>;
}

// The central statements by status, and how many passages are supporting.
interface CentralCounts {
  byStatus: Record<SummaryStatus, number>;
  supporting: number;
}

// The central statements' counts, from a summary's by_prominence; null when
// the store has none (built before it).
// A passage with no status counts in neither (ARTICLE_STATUSES).
function centralFromSummary(raw: Record<string, ProminenceCounts> | null | undefined): CentralCounts | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const byStatus = emptyStatusCounts();
  for (const prominence of CENTRAL_PROMINENCES) {
    const bucket = raw[prominence];
    for (const s of ARTICLE_STATUSES) byStatus[s] += count(bucket?.by_status?.[s]);
  }
  const supporting = Math.max(0, count(raw.supporting?.passages) - count(raw.supporting?.by_status?.unresolved));
  return { byStatus, supporting };
}

// The same, from an article's passages; null when no passage carries a
// prominence (a store built before every record had one). `statusOf` is the
// status the page is showing, so the line moves with the strip when community
// edits are folded in (plan section 2.8).
function centralFromPassages(
  passages: ArticlesPassage[],
  statusOf: (passage: ArticlesPassage) => SummaryStatus = passageStatus,
): CentralCounts | null {
  if (!passages.some((p) => typeof p.prominence === "string")) return null;
  const byStatus = emptyStatusCounts();
  for (const passage of passages) {
    if (CENTRAL_PROMINENCES.includes(passage.prominence as string)) byStatus[statusOf(passage)] += 1;
  }
  return { byStatus, supporting: passages.filter((p) => p.prominence === "supporting").length };
}

// Between the status counts of the central-statements line: a comma in an
// element of its own, so the summary box's ".articles-mini + .articles-mini" margin
// (for its space-joined counts) does not match across it, and a wrapped line
// starts flush.
const CENTRAL_SEPARATOR = "<span>,</span> ";

// "Central statements": how many of the passages state the article's subject
// or a named result or definition (prominence subject or central, proposal
// §2.2), how many are supporting, and the central ones by status. It follows
// the bar over all passages and that bar's own legend, so the first figures
// under the bar are the bar's. `articles` is the dashboard's article count.
function centralLine(central: CentralCounts, rawAll: number, scope: { object: string; articles: number | null }): string {
  const all = count(rawAll);
  if (all === 0) return "";
  const { byStatus, supporting } = central;
  // Only the statuses a passage can be shown with: both callers count none for a passage with no
  // status, and this keeps one from ever reaching the line.
  const total = ARTICLE_STATUSES.reduce((sum, s) => sum + byStatus[s], 0);
  let sentence: string;
  if (total === 0) {
    sentence = `No passage states ${text(scope.object)}.`;
  } else {
    const who = total === all ? (all === 1 ? "The one passage" : `All ${int(all)} passages`)
      : `${int(total)} of the ${plural(all, "passage", "passages")}`;
    const across = scope.articles === null ? "" : `, across ${plural(count(scope.articles), "article", "articles")},`;
    const others = supporting === all - total
      ? (supporting === 1 ? "the other one" : `the other ${int(supporting)}`) : int(supporting);
    const rest = supporting > 0 ? `; ${others} ${supporting === 1 ? "is" : "are"} supporting (examples, setup)` : "";
    const parts = ARTICLE_STATUSES.filter((s) => byStatus[s] > 0)
      .map((s) => `<span class="articles-mini" data-status="${s}"><span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(byStatus[s])} ${text(STATUS[s].plural)}</span>`)
      .join(CENTRAL_SEPARATOR);
    sentence = `${who}${across} ${total === 1 ? "states" : "state"} ${text(scope.object)}${rest}. ` +
      `Of ${total === 1 ? "this one" : `these ${int(total)}`}: ${parts}.`;
  }
  return `<p class="articles-strip-lead" id="articles-central"><b>Central statements:</b> ${sentence}</p>`;
}

// ---- shell -----------------------------------------------------------------

// The licences the footers name: the article text's (Wikipedia's, CC BY-SA 4.0) and the
// mapping's (WikiLean's, CC0), which each article's JSON states too (`licence`). Only the
// reader, whose content is the adapted revision, marks its licence rel="license".
const CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/";
const CC_BY_SA = `<a href="${CC_BY_SA_URL}">CC BY-SA 4.0</a>`;
const CC0 = `<a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>`;

// The footer's last paragraph, on every articles page: the mapping is CC0, apart from the text and
// from the Lean code on the cards, which each library's own licence covers (the universe's
// libraries name it, and the universe page lists it).
const MAPPING_LICENCE = `<p>The mapping (which passage is formalized where, in which library and how closely) is WikiLean&#x27;s and is released under ${CC0}, like WikiLean&#x27;s annotations; it is distinct from the article text, which keeps its own licence. The Lean code on the cards is quoted from its libraries and keeps each library&#x27;s own licence, which the universe page lists; the CC0 release does not cover it. Each location&#x27;s Details name the locate run that found it and the challenge run that re-checked it, if one did. Signed-in readers can confirm or report a location.</p>`;

// What the JSON downloads carry of the article's text, which stays under its licence.
function downloadsLine(fullText: boolean): string {
  return `${fullText ? "The quote and text fields of the JSON download and the quote fields of the JSONL download" : "The quote fields of the JSON and JSONL downloads"} are excerpts of the same text and stay under that licence.`;
}

// The licence an article's text is under, as its JSON states it (`licence.text`, its records'
// subject licence): a Creative Commons BY-SA version is named and linked, another id is named as
// the store gives it, and a store from before the field has Wikipedia's, CC BY-SA 4.0.
const TEXT_LICENCES: Record<string, { name: string; url: string }> = {
  "CC-BY-SA-4.0": { name: "CC BY-SA 4.0", url: CC_BY_SA_URL },
  "CC-BY-SA-3.0": { name: "CC BY-SA 3.0", url: "https://creativecommons.org/licenses/by-sa/3.0/" },
};
function textLicence(article: ArticlesArticle, rel = false): string {
  const id = article.licence && typeof article.licence === "object" ? article.licence.text : null;
  const known = typeof id === "string" ? TEXT_LICENCES[id] : TEXT_LICENCES["CC-BY-SA-4.0"];
  if (!known) return text(String(id));
  return `<a href="${attr(known.url)}"${rel ? ` rel="license"` : ""}>${text(known.name)}</a>`;
}

// The reader's images and media, as the store lists them (build_store reader_images): each file
// it shows, linked to its file page, with its author as that page credits them (the builder
// requires one wherever the licence asks for it), its project (Wikimedia Commons or English
// Wikipedia) and its licence, linked to the licence's own page where it has one. A store object is
// cast, so anything else is left out.
const FILE_PAGES: Record<string, { host: string; label: string }> = {
  commons: { host: "https://commons.wikimedia.org/wiki/File:", label: "Wikimedia Commons" },
  en: { host: "https://en.wikipedia.org/wiki/File:", label: "English Wikipedia" },
};
function readerImages(article: ArticlesArticle): string {
  const rows = list(article.reader && typeof article.reader === "object" ? article.reader.images : null)
    .filter((img) => img && typeof img === "object" && typeof img.file === "string" && img.file !== ""
      && Object.prototype.hasOwnProperty.call(FILE_PAGES, img.project))
    .map((img) => {
      const page = FILE_PAGES[img.project];
      const author = typeof img.author === "string" && img.author ? ` by ${text(img.author)}` : "";
      const url = typeof img.licence_url === "string" ? httpsUrl(img.licence_url) : null;
      const name = typeof img.licence === "string" && img.licence ? text(img.licence) : "";
      const licence = !name ? "its licence on its file page" : url ? `<a href="${attr(url)}">${name}</a>` : name;
      return `<li><a href="${attr(page.host + encodeURIComponent(img.file))}">${text(img.file.replaceAll("_", " "))}</a>${author} (${page.label}): ${licence}</li>`;
    });
  return rows.length
    ? `<details class="atlr-images"><summary>Images and media in this revision (${int(rows.length)}), each under its own licence</summary><ul>${rows.join("")}</ul></details>`
    : "";
}

// One article's attribution: its pinned revision, its contributors (the history) and the licence.
function articleCredit(article: ArticlesArticle): string {
  const history = `https://en.wikipedia.org/w/index.php?curid=${int(article.pageid)}&action=history`;
  return `<a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}">revision ${int(article.revision)}</a> of “${text(article.title)}” on <a href="https://en.wikipedia.org/">Wikipedia</a>, by <a href="${attr(history)}">its contributors</a>`;
}

function shell(opts: {
  title: string; description: string; canonical?: string; main: string; script?: boolean; dashboard?: boolean;
  // The page shows passages in full, so its JSON download carries their text.
  fullText?: boolean;
  // A page that has the article reader (articlesReaderPage, and its card list's view switch):
  // its stylesheet, a class on the body, a skip link before the header, a second header
  // row, its own footer and its script. Absent, the shell is what it always was.
  readerCss?: boolean; bodyClass?: string; skip?: string; headerRow?: string; footer?: string; readerJs?: boolean;
  // The page puts the notice line at the top of its own main column (the reader: under the
  // header still, but beside the sticky panel rather than above it, so the panel keeps the
  // viewport's height).
  noticeInMain?: boolean;
  // The page renders the Turnstile widget for a new account's first edit, so it
  // loads Cloudflare's script — the ONLY cross-origin script any page of this
  // plane loads, and only on this one page state. articlesCsp(origin, true)
  // widens script-src, frame-src and connect-src by exactly that origin.
  turnstile?: boolean;
}): string {
  // aria-current="page" only on /articles itself: an article or universe page is
  // inside the plane but is not the page the nav link opens.
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${text(opts.title)}</title>
<meta name="robots" content="noindex">
<meta name="description" content="${attr(opts.description)}">
${opts.canonical ? `<link rel="canonical" href="${attr(opts.canonical)}">\n` : ""}<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<link rel="stylesheet" href="${STYLE_CSS}">
<link rel="stylesheet" href="${ARTICLES_CSS}">
${opts.readerCss ? `<link rel="stylesheet" href="${ARTICLES_READER_CSS}">\n` : ""}<style>${SEARCHBOX_CSS}</style>
</head>
<body class="articles-body${opts.bodyClass ? ` ${attr(opts.bodyClass)}` : ""}">
${opts.skip ? `${opts.skip}\n` : ""}<header class="wl-header">
  <div class="wl-title">
    <a class="wl-brand" href="/">WikiLean</a>
    <span class="wl-sep">·</span>
    <a class="wl-article articles-home" href="/articles">Articles</a>
    <span class="wl-nav">
      ${SEARCHBOX_HTML}
      ${siteNav(opts.dashboard ? "/articles" : "")}
      <button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>
    </span>
  </div>
${opts.headerRow ? `${opts.headerRow}\n` : ""}</header>
${opts.noticeInMain ? "" : `${NOTICE_LINE}\n`}${opts.main}
${opts.footer ?? `<footer class="articles-foot">
  <p>Wikipedia text: ${CC_BY_SA}. WikiLean mappings: CC0. Lean code keeps its library’s licence.</p>
  <details class="articles-about"><summary>Sources, licences, and data</summary>
  <p>Quoted passages are excerpts of <a href="https://en.wikipedia.org/">Wikipedia</a> articles, available under ${CC_BY_SA}. ${downloadsLine(opts.fullText === true)}</p>
  ${MAPPING_LICENCE}
  </details>
  <p>${SITE_TOOLS}</p>
</footer>`}
${SEARCHBOX_SCRIPT}
${opts.turnstile ? `<script src="${TURNSTILE_SCRIPT}" async defer></script>\n` : ""}${opts.script === false ? "" : `<script src="${ARTICLES_JS}"></script>\n`}${opts.readerJs ? `<script src="${ARTICLES_READER_JS}"></script>\n` : ""}</body>
</html>
`;
}

export function articlesMessagePage(title: string, message: string, detail: string): string {
  return shell({
    title,
    description: message,
    main: `<main class="wl-articles articles-message">\n<h1 class="articles-h1">${text(title)}</h1>\n<p>${text(message)}</p>\n` +
      (detail ? `<p class="articles-muted">${text(detail)}</p>\n` : "") +
      `<p><a href="/articles">Articles</a> · <a href="/">WikiLean</a></p>\n</main>`,
  });
}

// ---- shared pieces -----------------------------------------------------------

interface LibraryView {
  id: string;
  info: LibraryInfo;
  commit: string | null;
}

// The page's libraries by id: the universe's, then any library a location names
// that the universe does not list (the route passes no universe when its object
// cannot be read). Keyed by id, as the store is, so libraries split from one
// repository (Init and Std of Lean core) are two views, named apart (libraryInfos).
function libraryViews(universe: ArticlesUniverse | null, extra: Iterable<{ library: string; repo: string; commit: string }>): Map<string, LibraryView> {
  const commits = new Map<string, string>();
  const entries: LibraryEntry[] = [];
  for (const lib of universeLibraries(universe)) {
    if (commits.has(lib.id)) continue;
    commits.set(lib.id, lib.commit);
    entries.push(lib);
  }
  for (const loc of extra) {
    if (commits.has(loc.library)) continue;
    commits.set(loc.library, loc.commit);
    entries.push({ id: loc.library, repo: loc.repo });
  }
  const infos = libraryInfos(entries);
  return new Map(entries.map((entry) => [entry.id, { id: entry.id, info: infos.get(entry.id) as LibraryInfo, commit: commits.get(entry.id) ?? null }]));
}

// Libraries by the name a reader sees, then by universe id, so a tie is stable.
function byName(a: LibraryView, b: LibraryView): number {
  return a.info.name.localeCompare(b.info.name, "en") || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

// A table that may scroll sideways on a phone: a labelled, focusable region,
// so a keyboard user can scroll it (axe scrollable-region-focusable). Its name
// says "table" so it differs from the section around it.
function tableWrap(label: string, table: string): string {
  return `<div class="articles-table-wrap" role="region" tabindex="0" aria-label="${attr(`Table: ${label}`)}">${table}</div>`;
}

// The bar over the statuses a passage can be shown with (ARTICLE_STATUSES): never a segment for
// passages with no status, whatever counts it is given.
function statusBar(raw: Record<SummaryStatus, number>, one: string, many: string): string {
  const counts = Object.fromEntries(ARTICLE_STATUSES.map((s) => [s, count(raw?.[s])])) as Record<SummaryStatus, number>;
  const total = ARTICLE_STATUSES.reduce((sum, s) => sum + counts[s], 0);
  if (total === 0) return "";
  const label = ARTICLE_STATUSES.filter((s) => counts[s]).map((s) => `${counts[s]} ${STATUS[s].plural}`).join(", ");
  const segments = ARTICLE_STATUSES.filter((s) => counts[s]).map((s) => {
    const pct = ((counts[s] / total) * 100).toFixed(2);
    return `<span class="articles-seg" data-status="${s}" style="width:${pct}%" title="${attr(`${counts[s]} ${STATUS[s].plural}`)}"></span>`;
  }).join("");
  return `<div class="articles-bar" role="img" aria-label="${attr(`${plural(total, one, many)}: ${label}`)}">${segments}</div>`;
}

// The location that justifies a located passage's status: the store's basis
// (build_store derive_status), or, for a store built before it existed, the
// highest-ranked location whose own status (locationStatus) is the passage's.
// Never simply the rank-1 location: rank 1 may state without a proof what
// rank 2 proves.
export function basisLocation(passage: ArticlesPassage, status: SummaryStatus, statusRule = 1): ArticlesLocation | null {
  const locations = list(passage.locations);
  if (passage.basis) {
    const hit = locations.find((loc) => loc.location_key === passage.basis);
    if (hit) return hit;
  }
  return locations.find((loc) => locationStatus(passage.kind, loc, statusRule) === status) ?? null;
}

// Decision D1: the hasPart locations a survived coverage claim names, in the
// claim's order; [] when there is no claim, it has not survived, or a part is
// not one of the passage's hasPart locations (the builder refuses that).
function coverageParts(passage: ArticlesPassage): ArticlesLocation[] {
  const claim = passage.coverage;
  if (!claim || claim.challenge !== "survived") return [];
  const locations = list(passage.locations);
  const parts = list(claim.parts).map((key) => locations.find((loc) => loc.location_key === key));
  return parts.length >= 2 && parts.every((loc) => loc?.relation === "hasPart") ? (parts as ArticlesLocation[]) : [];
}

// "Formalized by N parts together", with the parts listed: shown only for a
// survived claim. Its words follow what the claim gives on its own: formalized
// when every part is finished (proved, or data), statement only when one is
// not (build_store.coverage_status, the round-three reading of D1). A store
// built before that reading could leave a survived claim no status.
function coverageBlock(passage: ArticlesPassage, parts: ArticlesLocation[], libs: Map<string, LibraryView>): string {
  const n = parts.length;
  const status = passage.coverage?.status;
  // A /2 store's bundle names its relation, its weakest part's (exact, close or more general).
  const relation = typeof passage.coverage?.relation === "string" && passage.coverage.relation !== "exactMatch"
    ? ` (${RELATION_WORD[passage.coverage.relation] ?? passage.coverage.relation})` : "";
  const lead = status === "formalized" ? `Formalized by ${n} parts together${relation}`
    : status === "statement_only" ? `Stated by ${n} parts together${relation}, not all of them finished`
    : `Covered by ${n} parts together${relation}, not all of which count toward the status`;
  // Each part beside the clauses (conjuncts) it states, when the bundle names them.
  const conjuncts = list(passage.coverage?.conjuncts).filter((c) => c && typeof c === "object" && typeof c.text === "string");
  const items = parts.map((loc) => {
    const info = libs.get(loc.library)?.info ?? libraryInfo(loc.repo, null);
    const states = conjuncts.filter((c) => c.part === loc.location_key).map((c) => `<li>${text(c.text)}</li>`).join("");
    return `<li>${declName(String(loc.declaration ?? ""))} ` +
      `<span class="articles-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span> ` +
      `<span class="articles-muted">(${text(proofOf(loc).label)})</span>` +
      (states ? `<ul class="articles-conjuncts" aria-label="Clauses this part states">${states}</ul>` : "") + `</li>`;
  }).join("");
  return `<div class="articles-coverage" data-coverage-status="${attr(status ?? "none")}">` +
    `<p class="articles-why">${text(lead)}:</p><ol class="articles-coverage-parts">${items}</ol></div>`;
}

// The status in words, from the location that gives it. A partial status from an
// exact, close or more general match says why it is not formalized: a definition
// passage is formalized only by a Lean definition, so a proved theorem that states
// it (a characterisation) leaves it partial; and under status rules 1 and 2 a more
// general definition leaves a theorem partial (the section 2.6 table,
// build_store.location_status).
function statusSentence(
  passage: ArticlesPassage,
  status: SummaryStatus,
  libs: Map<string, LibraryView>,
  statusRule = 1,
  // The location that gives the status, when the caller already knows it (the
  // merged view picks it from the shown cards, which may be a community one).
  chosen?: ArticlesLocation | null,
  community = false,
): string {
  const meta = STATUS[status];
  const best = chosen !== undefined ? chosen : basisLocation(passage, status, statusRule);
  if (!best) return `${meta.label}.`;
  const name = libs.get(best.library)?.info.name ?? best.library;
  const by = community ? `${meta.label}, by a community edit: ${name}` : `${meta.label}: ${name}`;
  const states = `${by} ${RELATION_PHRASE[best.relation] ?? best.relation}`;
  if (status === "partial" && !PART_RELATIONS.has(best.relation)) {
    const kind = String(passage.kind ?? "");
    if (DEFINITION_KINDS.has(kind) && best.proof_status !== "definition") {
      return `${states}, but as a ${proofOf(best).label} theorem, not a definition; a ${kind} passage is formalized only by a Lean definition.`;
    }
    if (THEOREM_KINDS.has(kind) && best.relation === "broadMatch") {
      return `${states}, but as a definition, not a theorem; under status rule ${int(statusRule)} that leaves a ${kind} partial.`;
    }
  }
  return `${states} (${proofOf(best).phrase}).`;
}

// What the page needs in order to show, and take, community edits (the signed
// public-editing plan, sections 2.2 and 2.8; release R1). `merged` is the sealed
// article joined with its overlay; `show` is the view the reader chose, which
// defaults to "With community edits" (decision 4: otherwise an edit has no
// visible effect and nobody makes a second one).
export interface OverlayView {
  merged: MergedArticle;
  show: boolean;
  // The viewer may write: a signed-in person, or nobody.
  canEdit: boolean;
  // Their first overlay edit: the form carries the CC0 checkbox (decision 11)
  // and, when a site key is configured, the Turnstile widget.
  needsAgreement: boolean;
  // Their grant is on file but no Turnstile check cleared it, and a secret is
  // configured: the tag route asks for the check, so the form carries the
  // widget (and no checkbox — the licence is already granted).
  needsVerification: boolean;
  // The article's record set carries the /2 keys an overlay needs. A /1 set
  // cannot take a tag, so the page offers none and asks for no agreement.
  taggable: boolean;
  cc0Text: string;
  cc0Version: string;
  turnstileSiteKey: string | null;
  // The universe's libraries, for the picker: a tag may name only one of them.
  libraries: Array<{ id: string; label: string }>;
}

interface CardContext {
  libs: Map<string, LibraryView>;
  user: AuthUser | null;
  verdicts: Map<string, string>;
  loginHref: string;
  // The community overlay, or null on a deployment that has none to show.
  overlay: OverlayView | null;
  // The article's campaign token and seal summary, which the Details folds cite.
  campaign: string;
  seal: SealSummary | null;
  statusRule: number;
  // The runs' models, operator and dates when the store has them (--provenance).
  runs: ArticleRuns | null;
  // Where the store's signatures come from, when it has them (--signatures).
  signatures: ArticlesSupplements["signatures"] | null;
  // Each passage's full text as HTML (with the sentence around it, when it is only
  // part of one), or null where the page shows the quote.
  texts: Map<ArticlesPassage, PassageView | null>;
  // The article reader's URL when the store has the marked revision (--reader-html), and the
  // card list's: each card-list passage links to its place in the article, and each panel
  // passage to its card.
  readerHref?: string | null;
  cardsHref?: string;
}

interface PassageView { html: string; before: string; after: string }

// A passage's context (the rest of its sentence, or the sentence leading into a list
// item) as HTML: "" when the store has none or it does not render.
function contextHtml(raw: PassageContext | null | undefined, side: "before" | "after"): string {
  if (!raw || typeof raw !== "object") return "";
  const html = passageTextHtml(raw.segments);
  if (html === null || html.trim() === "") return "";
  const cut = raw.trimmed === true ? "…" : "";
  return side === "before" ? `${cut}${html}${raw.lead_in === true ? "<br>" : ""}` : `${html}${cut}`;
}

// The passage's verified full text as a view, or null (the page shows the quote).
function passageView(passage: ArticlesPassage): PassageView | null {
  const t = passage.text;
  if (!t || typeof t !== "object") return null;
  const html = passageTextHtml(t.segments);
  return html === null ? null : { html, before: contextHtml(t.before, "before"), after: contextHtml(t.after, "after") };
}

// A location's provenance, specific and behind a closed fold: the run that found
// it (with its rank and confidence there), the run that re-checked it, the
// export, what the record set does not record, and the keys an audit uses.
function locationDetails(loc: ArticlesLocation, ctx: CardContext): string {
  const runs = campaignRuns(ctx.campaign);
  const found = ctx.runs ? `Found ${runCite(ctx.runs.locate, "locate")}`
    : runs ? `Found in locate run <code>${text(runs.locate)}</code>` : `Found in campaign <code>${text(ctx.campaign)}</code>`;
  const day = dayOf(String(ctx.seal?.created ?? ""));
  const lines = [
    `${found}: candidate ${int(loc.rank)} for this passage, ${text(loc.confidence)} confidence.`,
    ctx.runs ? challengeCitation(String(loc.challenge ?? ""), ctx.runs.challenge, runs)
      : challengeSentence(String(loc.challenge ?? ""), runs),
  ];
  if (ctx.seal) {
    lines.push(`Record set <code>${text(String(ctx.seal.set_id ?? ""))}</code>${day ? `, exported ${text(day)}` : ""}` +
      ` by formal-companion <code title="${attr(String(ctx.seal.skill_commit ?? ""))}">${text(sha7(String(ctx.seal.skill_commit ?? "")))}</code>.`);
  }
  if (!ctx.runs) lines.push(UNRECORDED);
  if (signatureOf(loc) && ctx.signatures) {
    lines.push(`Lean code: the declaration&#x27;s type as Lean prints it, laid out as a header (the corpus keeps no hypothesis names), ` +
      `from the declaration corpus of universe ${text(String(ctx.signatures.universe ?? ""))} at commit ` +
      `<code title="${attr(String(loc.commit ?? ""))}">${text(sha7(String(loc.commit ?? "")))}</code>.`);
  }
  lines.push(`Location key <code title="${attr(loc.location_key)}">${text(String(loc.location_key ?? "").slice(0, 12))}</code>` +
    (typeof loc.pair_key === "string" ? ` · pair key <code title="${attr(loc.pair_key)}">${text(loc.pair_key.slice(0, 12))}</code>` : "") + ".");
  return detailsFold("articles-prov", "Details", lines);
}

// How the declaration relates to the passage, as a badge beside its name (a
// stroke per strength: solid for exact and close, dashed for more general,
// special case and part, dotted for related); the record's own term is its title.
function relationBadge(relation: string): string {
  const word = RELATION_WORD[relation] ?? relation;
  return `<span class="articles-rel" data-relation="${attr(relation)}" title="${attr(`${capital(RELATION_PHRASE[relation] ?? relation)} (${relation})`)}">${text(word)}</span>`;
}

// The relation words, each beside what it means, folded away under the Passages
// heading: the badges carry the word itself, and each one's title says what it means,
// so the list is for the reader who wants it and not a wall above the first passage.
function relationLegend(): string {
  const items = RELATION_ORDER.map((rel) => `<li>${relationBadge(rel)} ${text(RELATION_LEGEND[rel])}</li>`).join("");
  return `<details class="articles-legend-rel"><summary>What “exact match”, “more general” and the other labels mean</summary>` +
    `<ul>${items}</ul></details>`;
}

// The location's signature when the store has one (--signatures), checked to be
// strings; null otherwise, and the card shows the declaration's name alone.
function signatureOf(loc: ArticlesLocation): { text: string; full: string | null; deprecatedTo: string | null } | null {
  const sig = loc.signature;
  if (!sig || typeof sig !== "object" || typeof sig.text !== "string" || sig.text === "") return null;
  return {
    text: sig.text,
    full: sig.elided === true && typeof sig.type === "string" && sig.type !== "" ? sig.type : null,
    deprecatedTo: typeof sig.deprecated_to === "string" && sig.deprecated_to !== "" ? sig.deprecated_to : null,
  };
}

const SIG_OPEN = "([{⦃⟨⟪⁅⟦", SIG_CLOSE = ")]}⦄⟩⟫⁆⟧";

// A Lean header in the pieces a line may break between: the keyword and name, each
// binder group, the last binder with the header's colon, and after it each
// hypothesis with its arrow (or a clause with its comma), so the page flows it at any
// width without splitting a binder or a hypothesis unless one alone is wider than the
// line. A store's own line breaks are read as spaces.
export function signatureUnits(sig: string): string[] {
  const s = String(sig ?? "").replace(/\s*\n\s*/g, " ").trim();
  // Words at bracket depth 0 (a bracket group, with what is glued to it, is one word).
  const words: string[] = [];
  let depth = 0, start = 0;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (SIG_OPEN.includes(ch)) depth++;
    else if (SIG_CLOSE.includes(ch)) depth = Math.max(0, depth - 1);
    else if (ch === " " && depth === 0) {
      if (i > start) words.push(s.slice(start, i));
      start = i + 1;
    }
  }
  if (start < s.length) words.push(s.slice(start));
  const units: string[] = [];
  let current: string[] = [];
  // A header starts with its keyword; a bare type (a fold's full type) has no binders
  // before a colon and breaks after each arrow from the start.
  let header = /^(?:theorem|lemma|def|abbrev|instance|class|structure|inductive|axiom|opaque) /.test(s);
  const flush = () => { if (current.length) units.push(current.join(" ")); current = []; };
  words.forEach((word, n) => {
    if (header && word === ":") {
      // The header's colon ends the last binder's piece.
      if (current.length) current.push(word); else if (units.length) units[units.length - 1] += " :"; else current.push(word);
      flush();
      header = false;
      return;
    }
    current.push(word);
    if (header) {
      // "theorem Name" is one piece; each binder group after it is its own.
      if (n >= 1) flush();
      return;
    }
    if (word === "→" || word === "↔" || word.endsWith(",")) flush();
  });
  flush();
  return units;
}

function sigCode(sig: string): string {
  return `<code>${signatureUnits(sig).map((unit) => `<span class="articles-sig-u">${text(unit)}</span>`).join(" ")}</code>`;
}

// The Lean code block under the declaration's name: its header (binders before
// the colon, no proof), monospace and flowed at the card's width; a header
// shortened to fit keeps the whole type, as Lean prints it, under a fold, and a
// deprecated name says what replaced it.
function signatureBlock(loc: ArticlesLocation): string {
  const sig = signatureOf(loc);
  if (!sig) return "";
  return `<pre class="articles-sig">${sigCode(sig.text)}</pre>\n` +
    (sig.full ? `<details class="articles-sig-full"><summary>Full type, as Lean prints it</summary><pre class="articles-sig">${sigCode(sig.full)}</pre></details>\n` : "") +
    (sig.deprecatedTo ? `<p class="articles-deprecated">Deprecated at this commit, in favour of ${declName(sig.deprecatedTo)}.</p>\n` : "");
}

// ---- the community overlay on a card ---------------------------------------
//
// Jack's display rule holds for a community card exactly as it does for a
// pipeline one: the Lean code leads, provenance sits behind a closed Details
// fold, and NO voting data is shown — a card never says how many people agreed
// with it. What a community card adds is a badge saying where it came from and
// who is accountable for it, which is attribution, not a score.

// "1 Nov 2026" from a millisecond timestamp; "" when it is not one.
function dayOfMs(ms: unknown): string {
  return typeof ms === "number" && Number.isFinite(ms) && ms > 0 ? dayOf(new Date(ms).toISOString()) : "";
}

// Who wrote a row, linked. An agent is shown with its owner beside it — every
// agent names a human (PR #126's agent_tokens.owner_id), and that is who agreed
// to the licence and who answers for the edit.
function byLine(m: MergedLocation, verb: string): string {
  const tag = m.tag;
  if (!tag) return "";
  const author = m.author;
  const who = author
    ? `<a href="/u/${attr(encodeURIComponent(author.id))}">${text(author.name)}</a>`
    : `<span>${text(tag.added_by)}</span>`;
  const owner = author && author.actor === "agent" && author.ownerId
    ? ` — owned by <a href="/u/${attr(encodeURIComponent(author.ownerId))}">${text(author.ownerName ?? author.ownerId)}</a>`
    : "";
  const day = dayOfMs(verb === "Removed by" ? tag.removed_at : tag.updated_at);
  return `${text(verb)} ${who}${owner}${day ? ` on ${text(day)}` : ""}`;
}

const COMMUNITY_BADGE =
  `<span class="articles-community" title="Added by a WikiLean editor, not by the locate pipeline. ` +
  `It is live, attributed and one edit away from being corrected.">Community</span>`;
const AGENT_BADGE =
  `<span class="articles-community is-agent" title="Written by an AI agent under its owner&#x27;s account.">Agent</span>`;

// The line above a card the community touched: the badge, who, and — for a
// change — what the pipeline said before it.
function overlayLine(m: MergedLocation): string {
  if (!m.tag) return "";
  const badge = m.tag.actor_type === "agent" ? `${COMMUNITY_BADGE} ${AGENT_BADGE}` : COMMUNITY_BADGE;
  if (m.state === "removed") {
    return `<p class="articles-overlay-line is-removed">${badge} ${byLine(m, "Removed by")}` +
      `${m.tag.removed_reason ? `: ${text(m.tag.removed_reason)}` : ""}</p>`;
  }
  if (m.origin === "community") {
    return `<p class="articles-overlay-line">${badge} ${byLine(m, "Added by")}</p>`;
  }
  if (m.baseRelation) {
    return `<p class="articles-overlay-line">${badge} The pipeline said ${relationBadge(m.baseRelation)}; ` +
      `${byLine(m, "changed by")}.</p>`;
  }
  if (m.confirms) {
    return `<p class="articles-overlay-line is-confirmed">${badge} ${byLine(m, "Confirmed by")}.</p>`;
  }
  return `<p class="articles-overlay-line">${badge} ${byLine(m, "Edited by")}.</p>`;
}

// The per-card editing affordances: change this link, or say it is wrong. The
// buttons carry the row's current version, which the script sends back as
// base_version, so a card loaded before somebody else's edit cannot clobber it.
function cardActions(m: MergedLocation, ctx: CardContext, passageKey: string): string {
  const overlay = ctx.overlay;
  if (!overlay?.canEdit || !overlay.show) return "";
  const key = m.loc.pair_key;
  if (typeof key !== "string" || key === "") return "";
  const version = m.tag ? m.tag.version : 0;
  const common = `data-pair-key="${attr(key)}" data-passage-key="${attr(passageKey)}"` +
    ` data-version="${int(version)}" data-relation="${attr(m.loc.relation)}"` +
    ` data-declaration="${attr(String(m.loc.declaration ?? ""))}" data-library="${attr(m.loc.library)}"` +
    ` data-note="${attr(String(m.loc.note ?? ""))}"`;
  if (m.state === "removed") {
    return `<div class="articles-tag-actions" ${common}>` +
      `<button type="button" class="articles-tag-btn" data-action="restore">Restore this link</button>` +
      `<span class="articles-tag-msg" role="status"></span></div>`;
  }
  return `<div class="articles-tag-actions" ${common}>` +
    `<button type="button" class="articles-tag-btn" data-action="change" aria-expanded="false">Change</button>` +
    `<button type="button" class="articles-tag-btn" data-action="remove" aria-expanded="false">This link is wrong</button>` +
    `<span class="articles-tag-msg" role="status"></span></div>`;
}

// ---- community coverage bundles (issue #173) --------------------------------
//
// "These together state the passage": the overlay's answer to a passage two or
// more declarations state between them. Each link is then a hasPart, which on
// its own leaves the passage partial; the bundle says the parts together state
// every clause, and the merge counts it as one more location by the builder's
// own stand-in (articles-overlay mergedStatus). It is shown as one group, not
// as a card: the cards above it are the parts themselves.

// Who wrote a bundle, linked, as byLine does for a tag's card.
function bundleByLine(b: MergedBundle, verb: string): string {
  const author = b.author;
  const who = author
    ? `<a href="/u/${attr(encodeURIComponent(author.id))}">${text(author.name)}</a>`
    : `<span>${text(b.bundle.added_by)}</span>`;
  const owner = author && author.actor === "agent" && author.ownerId
    ? ` — owned by <a href="/u/${attr(encodeURIComponent(author.ownerId))}">${text(author.ownerName ?? author.ownerId)}</a>`
    : "";
  const day = dayOfMs(verb === "Withdrawn by" ? b.bundle.removed_at : b.bundle.updated_at);
  return `${text(verb)} ${who}${owner}${day ? ` on ${text(day)}` : ""}`;
}

// What a live bundle does to the passage, in words: the same three readings the
// store's own claim gets (coverageBlock), plus the one a withdrawn part leaves.
function bundleLead(b: MergedBundle): string {
  const n = b.parts.length;
  const relation = b.bundle.relation !== "exactMatch"
    ? ` (${RELATION_WORD[b.bundle.relation] ?? b.bundle.relation})` : "";
  if (!b.counts) {
    // A row whose parts cannot be read at all (nothing the route writes, but a
    // hand-written one) says so rather than counting to nothing out loud.
    if (n < MIN_BUNDLE_PARTS) return "This coverage claim no longer names the parts it was made of";
    return b.locations.some((m) => m === null)
      ? `These ${int(n)} parts no longer state the passage together: one of them is no longer a link here`
      : `These ${int(n)} parts no longer state the passage together: one of them no longer states part of it`;
  }
  if (b.status === "formalized") return `Formalized by ${int(n)} parts together${relation}`;
  if (b.status === "statement_only") return `Stated by ${int(n)} parts together${relation}, not all of them finished`;
  return `Covered by ${int(n)} parts together${relation}, not all of which count toward the status`;
}

// One live bundle: who said it, what it does, its parts and the clause each
// states, and the button that withdraws it.
function bundleBlock(b: MergedBundle, ctx: CardContext): string {
  const badge = b.bundle.actor_type === "agent" ? `${COMMUNITY_BADGE} ${AGENT_BADGE}` : COMMUNITY_BADGE;
  const withdrawn = b.state === "removed";
  const line = withdrawn
    ? `<p class="articles-overlay-line is-removed">${badge} ${bundleByLine(b, "Withdrawn by")}` +
      `${b.bundle.removed_reason ? `: ${text(b.bundle.removed_reason)}` : ""}</p>`
    : `<p class="articles-overlay-line">${badge} ${bundleByLine(b, "Added by")}</p>`;
  const items = b.parts.map((key, i) => {
    const m = b.locations[i];
    const states = b.conjuncts.filter((c) => c.part === key)
      .map((c) => `<li>${text(c.text)}</li>`).join("");
    const clauses = states
      ? `<ul class="articles-conjuncts" aria-label="Clauses this part states">${states}</ul>` : "";
    if (!m) return `<li class="is-gone"><span class="articles-muted">A link that is no longer here</span>${clauses}</li>`;
    const loc = m.loc;
    const info = ctx.libs.get(loc.library)?.info ?? libraryInfo(loc.repo, null);
    const part = loc.relation === BUNDLE_PART_RELATION ? "" :
      ` <span class="articles-muted">(${text(RELATION_WORD[loc.relation] ?? loc.relation)}, not a part)</span>`;
    return `<li>${declName(String(loc.declaration ?? ""))} ` +
      `<span class="articles-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span> ` +
      `<span class="articles-muted">(${text(proofOf(loc).label)})</span>${part}${clauses}</li>`;
  }).join("");
  const actions = ctx.overlay?.canEdit && ctx.overlay.show
    ? `<div class="articles-tag-actions" data-bundle-key="${attr(b.bundle.bundle_key)}" ` +
      `data-version="${int(b.bundle.version)}">` +
      (withdrawn
        ? `<button type="button" class="articles-tag-btn" data-action="bundle-restore">Put this back</button>`
        : `<button type="button" class="articles-tag-btn" data-action="bundle-withdraw" aria-expanded="false">Withdraw this</button>`) +
      `<span class="articles-tag-msg" role="status"></span></div>`
    : "";
  return `<div class="articles-coverage articles-bundle${withdrawn ? " is-removed" : ""}" ` +
    `data-bundle-key="${attr(b.bundle.bundle_key)}" data-coverage-status="${attr(b.status ?? "none")}" ` +
    `data-counts="${b.counts ? "true" : "false"}">` +
    `${line}<p class="articles-why">${text(bundleLead(b))}:</p>` +
    `<ol class="articles-coverage-parts">${items}</ol>${actions}</div>`;
}

// "These together state the passage": offered only where it can be true — a
// signed-in editor, the merged view, and two or more links that each state part
// of this passage. One clause per part, which is the shape a bundle's conjuncts
// take (each part states at least one).
function bundleForm(passage: ArticlesPassage, parts: MergedLocation[], ctx: CardContext): string {
  const overlay = ctx.overlay;
  const key = typeof passage.passage_key === "string" ? passage.passage_key : "";
  if (!overlay?.show || !overlay.canEdit || key === "" || parts.length < MIN_BUNDLE_PARTS) return "";
  const hex = passageHex(passage) || key.slice(0, 8);
  // Every candidate is offered, not the first five: a bundle names at most five
  // parts, and which five is the editor's choice. The script holds them to the
  // count, and the route refuses more.
  const rows = parts.map((m, i) => {
    const loc = m.loc;
    const info = ctx.libs.get(loc.library)?.info ?? libraryInfo(loc.repo, null);
    const box = `bundlepart-${attr(hex)}-${i}`;
    const clause = `bundleclause-${attr(hex)}-${i}`;
    return `<li><label for="${box}"><input type="checkbox" id="${box}" name="part" value="${attr(String(loc.pair_key ?? ""))}"> ` +
      `${declName(String(loc.declaration ?? ""))} <span class="articles-lib">${text(info.short)}</span></label>` +
      `<label for="${clause}">What it states</label>` +
      `<input id="${clause}" type="text" name="clause" maxlength="${MAX_TAG_NOTE}" autocomplete="off" ` +
      `placeholder="the clause this one states">` +
      `</li>`;
  }).join("");
  const rels = BUNDLE_RELATION_ORDER.map((rel) =>
    `<option value="${attr(rel)}"${rel === "exactMatch" ? " selected" : ""}>${text(RELATION_WORD[rel] ?? rel)} — ${text(RELATION_LEGEND[rel])}</option>`).join("");
  return `<div class="articles-bundle-new" data-passage-key="${attr(key)}">
<button type="button" class="articles-tag-open" aria-expanded="false" aria-controls="bundleform-${attr(hex)}">These together state the passage</button>
<form class="articles-bundle-form" id="bundleform-${attr(hex)}" hidden>
<fieldset><legend>Say which parts together state this passage</legend>
<p class="articles-muted">Tick the links that each state one part of this passage — two to ${MAX_BUNDLE_PARTS} of them — and say what each of them states. WikiLean then counts them together, instead of counting each as a part and leaving the passage partial. Your edit goes live at once, attributed to you, and anyone can correct it.</p>
<ol class="articles-bundle-parts">${rows}</ol>
<label for="bundlerel-${attr(hex)}">How the parts together match the passage</label>
<select id="bundlerel-${attr(hex)}" name="relation">${rels}</select>
<div class="articles-tag-buttons"><button type="submit" class="articles-tag-btn is-primary">Save</button><button type="button" class="articles-tag-btn articles-tag-cancel">Cancel</button></div>
<p class="articles-tag-msg" role="status"></p>
</fieldset>
</form>
</div>`;
}

// "Tag this passage": the one new write of release R1. The form is server
// rendered and inert until the page's script wires it, and it carries no inline
// handler and no inline script (articlesCsp lists neither).
function tagForm(passage: ArticlesPassage, ctx: CardContext): string {
  const overlay = ctx.overlay;
  const key = typeof passage.passage_key === "string" ? passage.passage_key : "";
  if (!overlay?.show || key === "") return "";
  const hex = passageHex(passage) || key.slice(0, 8);
  if (!overlay.canEdit) {
    return `<p class="articles-tag-signin"><a href="${attr(ctx.loginHref)}">Sign in to tag this passage</a></p>`;
  }
  const libs = overlay.libraries.map((lib) =>
    `<option value="${attr(lib.id)}">${text(lib.label)}</option>`).join("");
  const rels = RELATION_ORDER.map((rel) =>
    `<option value="${attr(rel)}"${rel === "exactMatch" ? " selected" : ""}>${text(RELATION_WORD[rel] ?? rel)} — ${text(RELATION_LEGEND[rel])}</option>`).join("");
  return `<div class="articles-tag" data-passage-key="${attr(key)}">
<button type="button" class="articles-tag-open" aria-expanded="false" aria-controls="tagform-${attr(hex)}">Tag this passage</button>
<form class="articles-tag-form" id="tagform-${attr(hex)}" hidden>
<fieldset><legend>Add a Lean declaration for this passage</legend>
<p class="articles-muted">Name the declaration that states this passage, and say how closely it matches. Your edit goes live at once, attributed to you, and anyone can correct it.</p>
<label for="taglib-${attr(hex)}">Library</label>
<select id="taglib-${attr(hex)}" name="library">${libs}</select>
<label for="tagdecl-${attr(hex)}">Declaration</label>
<input id="tagdecl-${attr(hex)}" name="declaration" type="text" maxlength="${MAX_DECL_CHARS}" autocomplete="off" spellcheck="false" placeholder="e.g. AddCommGroup">
<p class="articles-muted articles-tag-decl-note">The declaration&#x27;s full Lean name. WikiLean does not check that it exists before saving: the index it could check against covers Mathlib only, and you may be tagging another library.</p>
<label for="tagrel-${attr(hex)}">How it matches</label>
<select id="tagrel-${attr(hex)}" name="relation">${rels}</select>
<label for="tagnote-${attr(hex)}">Note (optional)</label>
<textarea id="tagnote-${attr(hex)}" name="note" maxlength="${MAX_TAG_NOTE}" rows="2"></textarea>
<p class="articles-muted">Please do not paste Lean source into the note: it keeps its library&#x27;s licence, and this field does not. At most ${MAX_TAG_NOTE} characters.</p>
<div class="articles-tag-buttons"><button type="submit" class="articles-tag-btn is-primary">Save tag</button><button type="button" class="articles-tag-btn articles-tag-cancel">Cancel</button></div>
<p class="articles-tag-msg" role="status"></p>
</fieldset>
</form>
</div>`;
}

// The CC0 agreement at the point of a person's first edit (decision 11), with
// the Turnstile widget beside it when a site key is configured. Rendered once
// per page, hidden until the script needs it. **The wording is for Jack to
// review**; the version string is recorded with the timestamp, so changing the
// wording in a way that needs re-acceptance is a version bump.
function agreementBlock(ctx: CardContext): string {
  const overlay = ctx.overlay;
  if (!overlay?.canEdit || !overlayAsksAgreement(overlay) || !overlay.taggable || !overlay.show) return "";
  const widget = overlay.turnstileSiteKey
    ? `<div class="cf-turnstile" data-sitekey="${attr(overlay.turnstileSiteKey)}"></div>`
    : "";
  // The licence is asked for only when it is not already granted; a grant that
  // never passed a Turnstile check brings the widget alone.
  const heading = overlay.needsAgreement ? "Before your first edit" : "One check before your next edit";
  const licence = overlay.needsAgreement
    ? `<p><label><input type="checkbox" id="articles-cc0-box"> ${text(overlay.cc0Text)}</label></p>\n`
    : "";
  return `<section class="articles-cc0" id="articles-cc0" data-version="${attr(overlay.cc0Version)}" hidden aria-labelledby="articles-cc0-h">
<h2 class="articles-h2" id="articles-cc0-h">${text(heading)}</h2>
${licence}${widget}<p class="articles-tag-msg" role="status" id="articles-cc0-msg"></p>
</section>`;
}

// The two views of an article's locations, as a segmented control: the sealed
// record set as published, and the same set with the community's edits folded
// in (the default — decision 4). Shown only once an article has an edit.
function overlaySwitch(selfHref: string, show: boolean, live: number, removed: number): string {
  const q = selfHref.includes("?") ? "&" : "?";
  return `<nav class="articles-overlay-switch" aria-label="Which edits to show">` +
    `<a href="${attr(selfHref)}${q}overlay=off"${show ? "" : ` aria-current="page"`}>As published</a>` +
    `<a href="${attr(selfHref)}"${show ? ` aria-current="page"` : ""}>With community edits</a>` +
    `<span class="articles-muted articles-overlay-count">${int(live)} community ${live === 1 ? "edit" : "edits"}` +
    `${removed ? `, ${int(removed)} removed` : ""} on this page</span></nav>`;
}

const MAX_DECL_CHARS = 240;
const MAX_TAG_NOTE = 500;
// Cloudflare's widget loader. Its origin is also in security.ts TURNSTILE_ORIGIN,
// which articlesCsp lists only for a page that renders the widget.
export const TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js";

// Does this page carry the agreement block at all? Either the licence is not
// granted yet, or it is but no Turnstile check cleared it on a deployment that
// asks for one.
function overlayAsksAgreement(overlay: OverlayView): boolean {
  return overlay.needsAgreement || overlay.needsVerification;
}

// Does this page render the Turnstile widget? (The agreement block, which only
// a signed-in person who still owes the licence or the check sees, and only
// with a site key.)
export function pageNeedsTurnstile(overlay: OverlayView | null | undefined): boolean {
  return !!overlay && overlay.canEdit && overlayAsksAgreement(overlay) && overlay.taggable && overlay.show
    && !!overlay.turnstileSiteKey;
}

// A location card leads with the Lean code: the declaration, its signature when
// the store has one (the export names the declaration only; build_store
// --signatures adds the header), then its module and lines. Then the library,
// commit, relation, proof status and the pinned source link; the convention
// note; provenance under a closed Details fold; and the reader's own
// Confirm/Report (never anyone else's).
function locationCard(m: MergedLocation, ctx: CardContext, passageKey = ""): string {
  const loc = m.loc;
  const view = ctx.libs.get(loc.library);
  const info = view?.info ?? libraryInfo(loc.repo, null);
  const src = sourceUrl(info, loc);
  const proof = proofOf(loc);
  const short = sha7(loc.commit);
  // end_line is optional (an index may record only the first line): "line N".
  const line = count(loc.line), end = count(loc.end_line);
  const lines = end && end !== line ? `lines ${line}–${end}` : `line ${line}`;
  const community = m.origin === "community";
  const verdict = ctx.verdicts.get(loc.location_key);
  // Confirm and Report are a verdict on a SEALED location: they seed the
  // benchmark and their provenance CHECK keeps them human labels. A community
  // card has no sealed record behind it, so it carries no verdict buttons —
  // disagreeing with one is an overlay edit, which is labelled and revertable.
  const feedback = community ? ""
    : ctx.user
    ? `<div class="articles-fb" data-location-key="${attr(loc.location_key)}">` +
      `<button type="button" class="articles-fb-btn" data-verdict="confirm" aria-pressed="${verdict === "confirm"}">` +
      `<span aria-hidden="true">✓ </span>Confirm</button>` +
      `<button type="button" class="articles-fb-btn" data-verdict="report" aria-pressed="${verdict === "report"}">` +
      `<span aria-hidden="true">⚑ </span>Report</button>` +
      `<span class="articles-fb-msg" role="status">${verdict ? `You ${verdict === "confirm" ? "confirmed" : "reported"} this link.` : ""}</span></div>`
    : `<a class="articles-fb-signin" href="${attr(ctx.loginHref)}">Sign in to confirm or report</a>`;
  const where = community
    ? `<p class="articles-module articles-muted">No module or line: an editor named the declaration, not a place in the source.</p>`
    : `<p class="articles-module">${text(loc.module)} · ${text(lines)}</p>`;
  const top = community
    ? `<div class="articles-card-top">
<span class="articles-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span>
</div>`
    : `<div class="articles-card-top">
<span class="articles-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span>
<code class="articles-commit" title="${attr(`commit ${loc.commit}`)}">@${text(short)}</code>
<span class="articles-ps${proof.alert ? " is-alert" : ""}" data-proof="${attr(loc.proof_status)}" title="${attr(capital(proof.phrase))}">${proof.alert ? `<span aria-hidden="true">! </span>` : ""}${text(proof.label)}</span>
${src ? `<a class="articles-src" href="${attr(src)}" rel="noopener" target="_blank">Source @${text(short)}<span aria-hidden="true"> ↗</span><span class="articles-sr"> (opens ${text(info.name)} at commit ${text(short)}, ${text(lines)})</span></a>` : `<span class="articles-src is-none">No source link</span>`}
</div>`;
  const classes = `articles-card${community ? " is-community" : ""}${m.state === "removed" ? " is-removed" : ""}`;
  const foot = `${feedback}${cardActions(m, ctx, passageKey)}`;
  return `<article class="${classes}" data-library="${attr(loc.library)}" data-relation="${attr(loc.relation)}" data-proof="${attr(loc.proof_status)}"${m.tag ? ` data-origin="${attr(m.origin)}" data-state="${attr(m.state)}"` : ""}>
<p class="articles-decl-line">${declName(String(loc.declaration ?? ""))} ${relationBadge(loc.relation)}</p>
${signatureBlock(loc)}${where}
${top}
${overlayLine(m)}${loc.note ? `<p class="articles-note"><span class="articles-k">${text(NOTE_LABEL[loc.relation] ?? "Note")}:</span> ${text(loc.note)}</p>\n` : ""}${community ? "" : locationDetails(loc, ctx)}
<div class="articles-card-foot">
${foot}
</div>
</article>`;
}

// The sealed status the store published.
function passageStatus(passage: ArticlesPassage): SummaryStatus {
  return passage.outcome === "unresolved" ? "unresolved" : asStatus(passage.status);
}

// The status the page shows: the merged one by default (decision 4 — otherwise
// an edit has no visible effect), the sealed one under "As published".
function shownStatus(passage: ArticlesPassage, ctx: CardContext): SummaryStatus {
  const overlay = ctx.overlay;
  if (!overlay?.show) return passageStatus(passage);
  return overlay.merged.byPassage.get(passage.id)?.status ?? passageStatus(passage);
}

// The locations the page shows for a passage, in card order: the pipeline's in
// rank order (each with the community's change applied, if there was one), then
// the community's own. Under "As published" it is the sealed list, untouched.
function shownLocations(passage: ArticlesPassage, ctx: CardContext): MergedLocation[] {
  const overlay = ctx.overlay;
  const merged = overlay?.show ? overlay.merged.byPassage.get(passage.id) : null;
  if (merged) return merged.locations;
  return list(passage.locations).map((loc) => ({
    loc, origin: "pipeline" as const, state: "live" as const, tag: null, author: null,
    baseRelation: null, baseNote: null, confirms: false,
  }));
}

// A passage the article views set aside: one with no status (articleParts).
function setAside(passage: ArticlesPassage): boolean {
  return passageStatus(passage) === "unresolved";
}

// The passage's anchor (#p-<hex>), from its item "In-text #<hex>"; "" if malformed.
function passageHex(passage: ArticlesPassage): string {
  return /#([0-9a-f]{8,16})$/.exec(String(passage.item ?? ""))?.[1] ?? "";
}

// A passage whose prominence is "subject" states what the article is about.
function isSubject(passage: ArticlesPassage): boolean {
  return passage.prominence === "subject";
}

// Between the pieces of a passage heading: read, never seen (the flex gap
// separates them on screen).
const HEADING_SEPARATOR = `<span class="articles-sr"> · </span>`;

// The shown location that gives a passage its shown status, or null: the
// store's own basis when it is still live, else the best-ranked shown card
// whose own status is the passage's.
function shownBasis(passage: ArticlesPassage, status: SummaryStatus, ctx: CardContext): MergedLocation | null {
  const live = shownLocations(passage, ctx).filter((m) => m.state === "live");
  const basis = basisLocation(passage, status, ctx.statusRule);
  return (basis ? live.find((m) => m.loc.location_key === basis.location_key) : undefined)
    ?? live.find((m) => locationStatus(String(passage.kind ?? ""), m.loc, ctx.statusRule) === status)
    ?? null;
}

// The location a passage's peek and index row lead with: the one that gives it its status, else
// its first; `more` counts its other locations (relatedMatch aside).
function leadOf(passage: ArticlesPassage, status: SummaryStatus, ctx: CardContext): { loc: ArticlesLocation; more: number } | null {
  const shown = shownLocations(passage, ctx).filter((m) => m.state === "live");
  const strong = shown.filter((m) => m.loc.relation !== "relatedMatch");
  const basis = basisLocation(passage, status, ctx.statusRule);
  // The basis is a sealed location; take the shown copy of it, which may carry
  // a community change, and fall back to the first strong card.
  const loc = (basis ? strong.find((m) => m.loc.location_key === basis.location_key)?.loc : undefined)
    ?? strong.find((m) => locationStatus(String(passage.kind ?? ""), m.loc, ctx.statusRule) === status)?.loc
    ?? strong[0]?.loc ?? null;
  return loc ? { loc, more: strong.length - 1 } : null;
}

// A passage's first words as plain text, each formula as its TeX: the label a highlight's
// screen-reader name and the peek use. The quote is the passage's head, in the layer's form.
export function plainHead(passage: Pick<ArticlesPassage, "quote">, max = 90): string {
  const words = quoteSegments(String(passage.quote?.exact ?? ""))
    .map((seg) => (seg.kind === "text" ? seg.text : seg.tex ?? seg.tokens))
    .join("").replace(/[\s\u200b-\u200d\u2060\ufeff]+/g, " ").trim();
  if (words.length <= max) return words;
  const cut = words.slice(0, max);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), max - 20)).trim()}…`;
}

// What the reader's script reads off a panel passage (data attributes, so it never parses
// HTML): its lead declaration, the library that holds it, how many more, the relation, the
// libraries an absence searched, and its first words.
function readerData(passage: ArticlesPassage, status: SummaryStatus, ctx: CardContext): string {
  const lead = leadOf(passage, status, ctx);
  const lib = lead ? (ctx.libs.get(lead.loc.library)?.info.short ?? lead.loc.library) : "";
  return (lead ? ` data-lead="${attr(String(lead.loc.declaration ?? ""))}" data-lead-lib="${attr(lib)}"` +
      ` data-more="${int(lead.more)}" data-rel="${attr(RELATION_WORD[lead.loc.relation] ?? lead.loc.relation)}"` : "") +
    (passage.absence ? ` data-searched="${int(passage.absence.libraries)}"` : "") +
    ` data-first="${attr(plainHead(passage))}"`;
}

// One passage: an item of the card list (`panel` false), or the reader's panel for it, a
// hidden section the reader's script shows when the passage is selected (`panel` true).
// Both hold the same heading, status sentence, text, cards and Details.
function passageBlock(passage: ArticlesPassage, article: ArticlesArticle, ctx: CardContext, panel = false): string {
  const status = shownStatus(passage, ctx);
  const sealedStatus = passageStatus(passage);
  const shown = shownLocations(passage, ctx);
  const liveShown = shown.filter((m) => m.state === "live");
  const gravestones = shown.filter((m) => m.state === "removed");
  const located = status === "formalized" || status === "statement_only" || status === "partial";
  const strong = liveShown.filter((m) => m.loc.relation !== "relatedMatch");
  const related = liveShown.filter((m) => m.loc.relation === "relatedMatch");
  const libraries = [...new Set(strong.map((m) => m.loc.library))];
  const ownKey = typeof passage.passage_key === "string" ? passage.passage_key : "";
  const hex = passageHex(passage);
  const subject = isSubject(passage);
  // A subject passage says so in its own tag, so its eyebrow is the kind alone.
  const eyebrow = [capital(passage.kind), subject ? "" : PROMINENCE_WORD[String(passage.prominence ?? "")] ?? passage.prominence]
    .filter(Boolean).join(" · ");
  const section = Array.isArray(passage.section) && passage.section.length ? passage.section.join(" › ") : "";
  const context = inContextUrl(passage);
  const quote = passage.quote;
  const parts: string[] = [];
  const prominence = typeof passage.prominence === "string" ? ` data-prominence="${attr(passage.prominence)}"` : "";
  // The producer's item id stays in the markup (data-item), not in the text.
  if (panel) {
    parts.push(`<section class="articles-passage atlr-pp${subject ? " is-subject" : ""}" id="atlr-pp-${hex}" data-p="${hex}" data-status="${status}"${prominence} data-libraries="${attr(libraries.join(" "))}" data-item="${attr(passage.item)}"${readerData(passage, status, ctx)} aria-labelledby="atlr-pp-h-${hex}" hidden>`);
  } else {
    parts.push(`<li class="articles-passage${subject ? " is-subject" : ""}" id="p-${hex}" data-status="${status}"${prominence} data-libraries="${attr(libraries.join(" "))}" data-item="${attr(passage.item)}">`);
  }
  // The heading's pieces are flex items with a visual gap only, and the section's
  // "§ " is CSS generated content, so a visually hidden " · " between them keeps
  // its accessible name from running together ("Theorem · central · § Statement").
  const heading = [
    `<span class="articles-eyebrow">${text(eyebrow)}</span>`,
    subject ? `<span class="articles-subject">Subject of the article</span>` : "",
    section ? `<span class="articles-section">${text(section)}</span>` : "",
  ].filter(Boolean).join(HEADING_SEPARATOR);
  parts.push(`<div class="articles-p-head"><h3 class="articles-p-h"${panel ? ` id="atlr-pp-h-${hex}" tabindex="-1"` : ""}>${heading}</h3>${pill(status)}</div>`);
  // The merged status is labelled as merged, and the published one is said
  // beside it, so a reader always knows which they are looking at (§2.8).
  if (status !== sealedStatus) {
    parts.push(`<p class="articles-overlay-line">With community edits: ${text(STATUS[status].label)}.` +
      ` As published it is ${text(STATUS[sealedStatus].label)}.</p>`);
  }
  // A located passage says, in words, which location gives it its status. An
  // absence says so in its own box below (a passage with no status is set aside,
  // articleParts, and never reaches here). When a
  // survived coverage claim gives it its status (D1), the claim's line, with
  // the parts listed, is that sentence; otherwise the line follows it.
  // A community removal of one part withdraws the claim: it no longer states
  // every clause, so the page stops showing it as one.
  const goneKeys = new Set(gravestones.map((m) => m.loc.location_key));
  const covering = located ? coverageParts(passage).filter((loc) => !goneKeys.has(loc.location_key)) : [];
  const claimWhole = covering.length === coverageParts(passage).length;
  const byClaim = claimWhole && covering.length > 0 && passage.coverage?.basis === true && passage.coverage.status === status;
  if (located && !byClaim) {
    const basis = shownBasis(passage, status, ctx);
    parts.push(`<p class="articles-why">${text(statusSentence(passage, status, ctx.libs, ctx.statusRule,
      basis ? basis.loc : null, basis !== null && basis.tag !== null))}</p>`);
  }
  if (covering.length && claimWhole) parts.push(coverageBlock(passage, covering, ctx.libs));
  // The quote verbatim, with its prefix and suffix as context. An ellipsis
  // after it means that the passage continues, and nothing else: it follows a
  // capped head (the passage goes on past the export's 12-word cap), outside
  // the highlight and after any suffix, unless the rest of the passage lies
  // within that suffix. A suffix is context and has no ellipsis of its own.
  // Its formulas render once each, from their TeX (articles-math renderQuote): the
  // layer holds each twice, as MathML token text and as the TeX annotation.
  // With the store's verified text (--passage-text) the passage is shown in full
  // instead, every formula drawn once from its TeX.
  // A passage that is only part of a sentence is highlighted, the rest of the sentence
  // around it in grey, as a quote's prefix and suffix are.
  const full = ctx.texts.get(passage) ?? null;
  let shownText: string;
  if (full !== null) {
    const inContext = full.before !== "" || full.after !== "";
    shownText = `<blockquote class="articles-quote articles-text" data-quote="full"${inContext ? ` data-context=""` : ""}>` +
      (full.before ? `<span class="articles-affix">${full.before}</span>` : "") +
      (inContext ? `<mark>${full.html}</mark>` : full.html) +
      (full.after ? `<span class="articles-affix">${full.after}</span>` : "") + `</blockquote>`;
  } else {
    const capped = quoteCapped(passage);
    const shown = renderQuote(quote, capped && !endsInSuffix(passage));
    shownText = `<blockquote class="articles-quote" data-quote="${capped ? "head" : "whole"}">` +
      (shown.prefix ? `<span class="articles-affix">…${shown.prefix}</span>` : "") +
      `<mark>${shown.exact}</mark>` +
      (shown.suffix ? `<span class="articles-affix">${shown.suffix}</span>` : "") +
      (capped && !endsInSuffix(passage) ? QUOTE_CONTINUES : "") +
      `</blockquote>`;
  }
  // In the panel the text is clamped to three lines, with a button the script shows when it is longer.
  parts.push(panel ? `<div class="atlr-clamp">${shownText}</div><button type="button" class="atlr-more" aria-expanded="false" hidden>Show all</button>` : shownText);
  const wikipedia = context ? `<a href="${attr(context)}" rel="noopener" target="_blank">Read it in context on Wikipedia<span aria-hidden="true"> ↗</span></a>` : "";
  if (!panel && ctx.readerHref) {
    parts.push(`<p class="articles-context"><a href="${attr(ctx.readerHref)}#p-${hex}">Show in article</a>${wikipedia ? ` · ${wikipedia}` : ""}</p>`);
  } else if (!panel && wikipedia) {
    parts.push(`<p class="articles-context">${wikipedia}</p>`);
  }
  if (strong.length) {
    parts.push(`<div class="articles-cards">${strong.map((m) => locationCard(m, ctx, ownKey)).join("\n")}</div>`);
  }
  // The absence box stays whatever the overlay says: the sealed set is always
  // shown as the sealed set (plan section 2.8). What moves is the status above
  // it, and the community card beside it says who disagreed.
  if (passage.absence) {
    const a = passage.absence;
    parts.push(`<p class="articles-absence"><span class="articles-ico" aria-hidden="true">✕</span>` +
      `Searched ${int(a.libraries)} libraries — not found. ` +
      `<a href="/articles/universe/${attr(a.universe_sha256)}">Which libraries, at which commits</a> ` +
      `(pinned ${text(article.universe.frozen)}).</p>`);
    const runs = campaignRuns(ctx.campaign);
    const searched = ctx.runs ? `Searched ${runCite(ctx.runs.locate, "locate")}`
      : runs ? `Searched in locate run <code>${text(runs.locate)}</code>` : `Searched in campaign <code>${text(ctx.campaign)}</code>`;
    parts.push(detailsFold("articles-prov articles-absence-prov", "Details", [
      `${searched}: ` +
        `the search receipt lists ${int(a.receipt?.queries)} queries and ${int(a.receipt?.candidates)} candidates examined` +
        ` (receipt <code title="${attr(String(a.receipt?.sha256 ?? ""))}">${text(String(a.receipt?.sha256 ?? "").slice(0, 12))}</code>).`,
      ctx.runs ? challengeCitation(String(a.challenge ?? ""), ctx.runs.challenge, runs, "upheld")
        : challengeSentence(String(a.challenge ?? ""), runs),
      ...(ctx.runs ? [] : [UNRECORDED]),
    ]));
  }
  if (related.length && panel) {
    parts.push(`<details class="atlr-related"><summary class="articles-related-h">Related, but states something else (${int(related.length)})</summary>` +
      `<div class="articles-cards is-related">${related.map((m) => locationCard(m, ctx, ownKey)).join("\n")}</div></details>`);
  } else if (related.length) {
    parts.push(`<h4 class="articles-related-h">Related, but states something else</h4>`);
    parts.push(`<div class="articles-cards is-related">${related.map((m) => locationCard(m, ctx, ownKey)).join("\n")}</div>`);
  }
  // Removed links are folded away, never deleted: the reason, the remover and
  // one click to put the link back (plan section 2.8).
  if (gravestones.length) {
    parts.push(`<details class="articles-removed"><summary class="articles-related-h">` +
      `Removed by an editor (${int(gravestones.length)})</summary>` +
      `<div class="articles-cards is-removed">${gravestones.map((m) => locationCard(m, ctx, ownKey)).join("\n")}</div></details>`);
  }
  // The community's coverage bundles over the cards above (issue #173), and the
  // form that makes one. Both are the merged view's: "As published" shows the
  // sealed set alone, which has no bundle of this kind in it.
  const merged = ctx.overlay?.show ? ctx.overlay.merged.byPassage.get(passage.id) : null;
  for (const bundle of merged?.bundles ?? []) parts.push(bundleBlock(bundle, ctx));
  const bundleParts = liveShown.filter((m) => m.loc.relation === BUNDLE_PART_RELATION
    && typeof m.loc.pair_key === "string" && m.loc.pair_key !== "");
  const bundling = bundleForm(passage, bundleParts, ctx);
  if (bundling) parts.push(bundling);
  const form = tagForm(passage, ctx);
  if (form) parts.push(form);
  if (panel) {
    parts.push(`<p class="atlr-links"><a href="${attr(ctx.cardsHref ?? "")}#p-${hex}">Open in card list</a>` +
      ` · <button type="button" class="atlr-copy" data-p="${hex}">Copy link</button>` +
      (context ? ` · <a href="${attr(context)}" rel="noopener" target="_blank">Read on Wikipedia<span aria-hidden="true"> ↗</span></a>` : "") +
      ` <span class="atlr-copy-msg" role="status"></span></p>`);
    parts.push(`</section>`);
  } else {
    parts.push(`</li>`);
  }
  return parts.join("\n");
}

function chip(filter: string, label: string, n: number, title: string, status?: SummaryStatus, icon?: string): string {
  const mark = (status ? swatch(status) : "") + (icon ? `<span class="articles-ico" aria-hidden="true">${icon}</span>` : "");
  // A filter that would show nothing is not offered: a zero count is inert.
  if (count(n) === 0) {
    return `<span class="articles-chip is-static is-zero"${status ? ` data-status="${status}"` : ""}>` +
      `${mark}${text(label)} <span class="articles-n">0</span></span>`;
  }
  return `<button type="button" class="articles-chip" data-filter="${attr(filter)}"${status ? ` data-status="${status}"` : ""} aria-pressed="false" title="${attr(title)}">` +
    `${mark}${text(label)} <span class="articles-n">${int(n)}</span></button>`;
}

// ---- the head of an article page: counts, legend, filters, About ------------
//
// Between the pieces of the counts line: a dot seen and not read, so the line
// reads "3 passages 1 formalized 1 not formalized" rather than "3 passages
// middle dot 1 formalized".
const COUNT_DOT = `<span class="articles-dot" aria-hidden="true">·</span>`;

// The one line under the title: how many passages the page shows, then each status
// it shows them with. A status no passage has is left out; the passages with no
// status yet are counted apart, in the muted tail, as they are everywhere else.
// `lead` replaces the passage count at its head (the list page leads with its articles).
function countsLine(byStatus: Record<SummaryStatus, number>, nUnread: number, lead: string[] = []): string {
  const total = ARTICLE_STATUSES.reduce((sum, s) => sum + count(byStatus[s]), 0);
  const head = lead.length ? lead : [`<b>${plural(total, "passage", "passages")}</b>`];
  const parts = ARTICLE_STATUSES.filter((s) => count(byStatus[s]) > 0).map((s) =>
    `<span class="articles-mini" data-status="${s}"><span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>` +
    `${int(byStatus[s])} ${text(STATUS[s].plural)}</span>`);
  return `<p class="articles-counts" id="articles-counts">${[...head, ...parts].join(COUNT_DOT)}</p>`;
}

// The legend under that line, which is also the filter: one swatch, icon and word
// per status, pressable (both scripts read data-filter and set aria-pressed). A
// status no passage has is inert, as a zero chip always was, and what each status
// means is the button's title and the "About this page" fold's first section.
function statusKey(byStatus: Record<SummaryStatus, number>): string {
  const chips = ARTICLE_STATUSES.map((s) => {
    const mark = `${swatch(s)}<span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>`;
    if (count(byStatus[s]) === 0) {
      return `<span class="articles-chip is-static is-zero" data-status="${s}">${mark}${text(STATUS[s].label)}</span>`;
    }
    return `<button type="button" class="articles-chip" data-filter="status:${s}" data-status="${s}" aria-pressed="false"` +
      ` title="${attr(`${STATUS[s].label}: ${READER_LEGEND[s]}. Show only these passages.`)}">${mark}${text(STATUS[s].label)}</button>`;
  }).join("");
  return `<div class="articles-chips articles-key" role="group" aria-label="Show passages by status">${chips}</div>`;
}

// The line a filter writes, and the button that clears it. The reader's script finds
// them by id, the card list's by class; both are here so the two pages read alike.
function filterNote(ids: boolean): string {
  return `<p class="articles-filter-note" role="status"><span class="articles-filter-text"${ids ? ` id="atlr-filter-text"` : ""}></span> ` +
    `<button type="button" class="articles-clear"${ids ? ` id="atlr-clear"` : ""} hidden>Show all passages</button></p>`;
}

// The filters that are not the legend — by library, and, in the reader, the
// article's own statements and the dimming of the text around them — folded away,
// so the page opens with the article and not with a bar of eleven buttons.
function moreFilters(libraryChips: string, extra: string): string {
  if (!libraryChips && !extra) return "";
  const row = (id: string, label: string, chips: string) =>
    `<div class="articles-strip-row"><span class="articles-strip-h" id="${id}">${text(label)}</span>` +
    `<div class="articles-chips" role="group" aria-labelledby="${id}">${chips}</div></div>`;
  return `<details class="articles-filters"><summary>More ways to filter</summary>` +
    (libraryChips ? row("articles-h-where", "Library", libraryChips) : "") +
    (extra ? row("articles-h-more", "Passages", extra) : "") + `</details>`;
}

// What the status colours mean, with a sample of each: the legend's long form.
function colourLegend(): string {
  return `<ul class="articles-meanings">${ARTICLE_STATUSES.map((s) =>
    `<li><span class="articles-sample" data-status="${s}"><span aria-hidden="true">${STATUS[s].icon} </span>${text(STATUS[s].label)}</span> ` +
    `${text(READER_LEGEND[s])}</li>`).join("")}</ul>`;
}

// One "About this page" fold, closed, holding every figure that is not the counts:
// the pinned revision, the libraries searched, what an audit measured, the runs and
// how the text is shown. Empty sections are left out rather than shown empty.
function aboutFold(sections: Array<{ h: string; body: string; id?: string }>, summary = "About this page"): string {
  const body = sections.filter((section) => section.body).map((section) =>
    `<section class="articles-about-sec"${section.id ? ` aria-labelledby="${section.id}"` : ""}>` +
    `<h2 class="articles-about-h"${section.id ? ` id="${section.id}"` : ""}>${text(section.h)}</h2>${section.body}</section>`).join("");
  return `<details class="articles-about articles-page-about" id="articles-about"><summary>${text(summary)}</summary>` +
    `<div class="articles-about-body">${body}</div></details>`;
}

// A list of "About" lines, the shape detailsFold gives them inside its own fold.
function provList(lines: string[]): string {
  return lines.length ? `<ul class="articles-prov-list">${lines.map((line) => `<li>${line}</li>`).join("")}</ul>` : "";
}

// ---- the article coverage page ---------------------------------------------

export interface ArticlePageInput {
  article: ArticlesArticle;
  universe: ArticlesUniverse | null;
  user: AuthUser | null;
  verdicts: Map<string, string>;
  wikileanSlug: string | null;
  selfHref: string;
  // The article reader's URL when the store has the article's marked revision (--reader-html):
  // the card list then carries the view switch and a "Show in article" link per passage.
  readerHref?: string | null;
  // The community overlay (release R1): the merged cards, the community badge,
  // the view switch and the merged derived status. Absent on a deployment with
  // no overlay to show.
  overlay?: OverlayView | null;
}

// What the card list and the reader share: the card context, and the pieces of the page
// head — the counts line, the legend that filters, the library chips and the one
// "About this page" fold — which the two views then place in their own layouts.
function articleParts(input: ArticlePageInput) {
  const { article, universe, user, verdicts, selfHref, wikileanSlug } = input;
  const readerHref = input.readerHref ?? null;
  const libs = libraryViews(universe, list(article.passages).flatMap((p) => list(p?.locations)));
  const seal = article.seal && typeof article.seal === "object" ? article.seal : null;
  const statusRule = seal && Number.isSafeInteger(seal.status_rule) ? (seal.status_rule as number) : 1;
  const supplements = article.supplements && typeof article.supplements === "object" ? article.supplements : null;
  const runs = articleRuns(article);
  // A passage with no status (the locate run settled none: an unresolved passage) is set aside
  // until someone reads it (the maintainer's decision, 2026-11-02, after PD5): neither view lists,
  // highlights or labels it, and the counts the pages show leave it out. The data keep it.
  const all = list(article.passages);
  const passages = all.filter((passage) => !setAside(passage));
  const nUnread = all.length - passages.length;
  // Each passage's verified full text (--passage-text), rendered once; null shows the quote.
  const texts = new Map(passages.map((passage) => [passage, passageView(passage)] as const));
  const ctx: CardContext = {
    libs, user, verdicts, loginHref: `/login?returnTo=${encodeURIComponent(selfHref)}`,
    overlay: input.overlay ?? null,
    campaign: String(article.campaign ?? ""), seal, statusRule, runs,
    signatures: supplements?.signatures && typeof supplements.signatures === "object" ? supplements.signatures : null,
    texts,
    ...(readerHref ? { readerHref, cardsHref: `${readerHref}?view=cards` } : {}),
  };
  const summary = article.summary;
  const n = Math.max(0, count(summary.passages) - nUnread);
  // The related declarations of a passage set aside are set aside with it.
  const unreadRelated = new Map<string, number>();
  for (const passage of all.filter(setAside)) {
    for (const loc of list(passage.locations)) {
      if (loc.relation === "relatedMatch") unreadRelated.set(loc.library, (unreadRelated.get(loc.library) ?? 0) + 1);
    }
  }
  const nLocations = count(summary.locations);
  const nRelated = Math.max(0, count(summary.related) - [...unreadRelated.values()].reduce((a, b) => a + b, 0));
  // Under "With community edits" the strip counts the merged statuses, so the
  // bar and the chips move with the page's cards (decision 4). The merge counts
  // only the passages a view shows; the summary's are the store's own.
  const byStatus: Record<SummaryStatus, number> = { ...emptyStatusCounts(), unresolved: 0 };
  if (ctx.overlay?.show && ctx.overlay.merged.edits > 0) {
    for (const passage of passages) {
      const s = shownStatus(passage, ctx);
      if (s !== "unresolved") byStatus[s] += 1;
    }
  } else {
    for (const s of ARTICLE_STATUSES) byStatus[s] = count(summary.by_status?.[s]);
  }
  const assessment = article.assessment;
  const assessed = assessment && (assessment.class || assessment.importance)
    ? `WikiProject Mathematics: ${[assessment.class ? `${assessment.class}-class` : "", assessment.importance ? `${importanceLabel(assessment.importance)} importance` : ""].filter(Boolean).join(", ")}`
    : "";
  const counts = countsLine(byStatus, nUnread);
  const key = statusKey(byStatus);
  const libraryChips = [...libs.values()]
    .filter((view) => summary.by_library?.[view.id])
    .map((view) => {
      const counts = summary.by_library[view.id];
      const commit = view.commit ? ` at commit ${sha7(view.commit)}` : "";
      const passages = count(counts.passages), locations = count(counts.locations);
      const related = Math.max(0, count(counts.related) - (unreadRelated.get(view.id) ?? 0));
      if (!passages && !related) return "";
      if (!passages) {
        return `<span class="articles-chip is-static" title="${attr(`${view.info.name}${commit}: ${plural(related, "related declaration", "related declarations")} only`)}">${text(view.info.short)} <span class="articles-n">related only</span></span>`;
      }
      return chip(`library:${view.id}`, view.info.short, passages,
        `${view.info.name}${commit}: ${plural(passages, "passage", "passages")}, ${plural(locations, "location", "locations")}. Show only those passages.`);
    }).join("");
  const nLibraries = int(article.universe.libraries);
  const nExcluded = universe ? list(universe.excluded).length : 0;
  // Plain words, in the About fold: how many Lean libraries were searched, at which
  // day's commits, and how many were left out — never "universe" or "frozen".
  const searched = universe
    ? `Searched <a href="/articles/universe/${attr(article.universe.sha256)}">${nLibraries} Lean libraries</a>, pinned ${text(article.universe.frozen)}${nExcluded ? `, ${int(nExcluded)} left out` : ""} (library set <code>${text(article.universe.id)}</code>).`
    : `Searched ${nLibraries} Lean libraries, pinned ${text(article.universe.frozen)} (library set <code>${text(article.universe.id)}</code>).`;
  // The article's subject passages, named in the About fold with a link to each.
  // A capped head's ellipsis follows the closing quotation mark, outside the quote.
  const subjects = passages.filter(isSubject).map((passage) => {
    const hex = passageHex(passage);
    const exact = renderQuote({ exact: String(passage.quote?.exact ?? "") }, quoteCapped(passage)).exact;
    const label = `${text(capital(String(passage.kind ?? "")))}: “${exact}”` + (quoteCapped(passage) ? QUOTE_CONTINUES : "");
    return `<li>${hex ? `<a href="#p-${hex}">${label}</a>` : label} ${pill(shownStatus(passage, ctx))}</li>`;
  });
  const subjectRow = subjects.length
    ? `<div class="articles-strip-row"><span class="articles-strip-h" id="articles-h-subject">Subject</span><ul class="articles-subject-list" aria-labelledby="articles-h-subject">${subjects.join("")}</ul></div>`
    : "";
  const central = centralFromPassages(passages, (passage) => shownStatus(passage, ctx));
  const centralRow = central ? centralLine(central, n, { object: "the article's subject or a named result or definition", articles: null }) : "";
  const nText = [...texts.values()].filter((view) => view !== null).length;
  const lead = `${plural(n, "passage", "passages")} checked${nLocations ? `, ${plural(nLocations, "Lean location", "Lean locations")}` : ""}${nRelated ? ` (and ${plural(nRelated, "related declaration", "related declarations")})` : ""}.`;
  const revisionLink = `<a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}" rel="noopener">${int(article.revision)}</a>`;
  const description = `Where ${article.title} is formalized in Lean: ${int(summary.by_status?.formalized)} of ${plural(n, "passage", "passages")} formalized.`;
  // How the passages are shown: in full from the store's verified text, or as the
  // export's capped quote; and where a card's Lean code comes from. Both views say it
  // in the About fold, never above the article.
  const anyCapped = passages.some((passage) => texts.get(passage) === null && quoteCapped(passage));
  const nContext = [...texts.values()].filter((view) => view !== null && (view.before !== "" || view.after !== "")).length;
  const textNote = nText
    ? `<p>${nText === n ? "Each passage is" : `${plural(nText, "passage is", "passages are")}`} shown in full, as revision ${int(article.revision)} states it (checked against its record&#x27;s span hash when the store was built).` +
      (nContext ? ` Where a passage is only part of a sentence, it is highlighted and the rest of the sentence is shown in grey around it, from the same copy of the revision; a list item also shows the sentence that introduces it.` : "") +
      ` Formulas are drawn from their TeX; hover one to see it.</p>`
    : "";
  const anySignature = passages.some((passage) => list(passage.locations).some((loc) => signatureOf(loc) !== null));
  const sigNote = anySignature && ctx.signatures
    ? `<p>Each card&#x27;s Lean code is its declaration&#x27;s type as Lean prints it, from the declaration corpus of library set ${text(String(ctx.signatures.universe ?? ""))} at the card&#x27;s commit, laid out as a header: binders before the colon, then the hypotheses as arrows (the corpus keeps no hypothesis names) and the conclusion, in Mathlib&#x27;s notation (𝓝, π, R[X], Type*). It is not the source text; Source opens that.</p>`
    : "";
  const quoteNote = anyCapped
    ? `<p>Quotes show at most the first ${QUOTE_MAX_WORDS} words of a passage (the record format's quotation cap; a formula's MathML text counts toward it). A … after the highlighted quote means the passage continues; “Read it in context on Wikipedia” shows the rest. Hover a formula for its TeX.</p>`
    : "";
  const about = aboutFold([
    { h: "This page", body: provList([
      `Wikipedia revision ${revisionLink}, pinned: the text here is that revision, not today&#x27;s article.`,
      ...(assessed ? [text(assessed)] : []),
      ...(wikileanSlug ? [`<a href="/${attr(encodeURIComponent(wikileanSlug))}">Annotated article on WikiLean</a>.`] : []),
      lead,
    ]) + centralRow + subjectRow },
    { h: "What the colours mean", body: colourLegend() },
    { h: "Libraries searched", body: `<p>${searched}</p>` },
    { h: "How these links were made", body: provList([...auditLines(article.audit),
      ...recordSetLines(String(article.campaign ?? ""), seal, String(article.record_set ?? ""), runs !== null),
      ...(runs ? [...(runs.inventory ? [runLine(runs.inventory)] : []), runLine(runs.locate),
        ...(runs.challenge ? [runLine(runs.challenge, recheckTallies(passages), "In this article it")] : [])] : [UNRECORDED]),
      ...supplementLines(supplements, nText, n)]) },
    { h: "How the text is shown", body: `${textNote}${sigNote}${quoteNote}` },
  ]);
  // The "As published" / "With community edits" control, offered only once the
  // article has an edit; the agreement block, rendered once per page.
  const overlay = ctx.overlay;
  const overlayRow = overlay && overlay.merged.edits > 0
    ? `${overlaySwitch(selfHref, overlay.show, overlay.merged.live, overlay.merged.removed)}\n`
    : "";
  const agreement = agreementBlock(ctx);
  // Only what a view places: the figures themselves are already built into `counts`,
  // `key` and `about` above, so neither page can say one of them a second time.
  return { ctx, n, counts, key, libraryChips, nText, about, description, readerHref, passages, nUnread, overlayRow, agreement };
}

// The two views of an article with a marked revision, as a segmented control: the article
// reader and the card list (the reader's script carries a selected passage across).
function viewSwitch(readerHref: string, current: "article" | "cards"): string {
  return `<nav class="atlr-switch" aria-label="View">` +
    `<a id="atlr-to-article" href="${attr(readerHref)}"${current === "article" ? ` aria-current="page"` : ""}>Article</a>` +
    `<a id="atlr-to-cards" href="${attr(readerHref)}?view=cards"${current === "cards" ? ` aria-current="page"` : ""}>Card list</a></nav>`;
}

export function articlesArticlePage(input: ArticlePageInput): string {
  const { article, user, selfHref } = input;
  const {
    ctx, counts, key, libraryChips, nText, about, description, readerHref, passages, nUnread,
    overlayRow, agreement,
  } = articleParts(input);
  const main = `<main class="wl-articles" id="articles-article" data-pageid="${int(article.pageid)}" data-campaign="${attr(article.campaign)}">
<nav class="articles-crumbs" aria-label="Breadcrumb"><a href="/articles">Articles</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="articles-h1">${text(article.title)}</h1>
${counts}
${key}
${filterNote(false)}
${moreFilters(libraryChips, "")}
${about}
${overlayRow}${agreement ? `${agreement}\n` : ""}<h2 class="articles-h2" id="articles-h-passages">Passages</h2>
${passages.length ? `${relationLegend()}\n` : ""}${passages.length ? `<ol class="articles-passages" aria-labelledby="articles-h-passages">\n${passages.map((p) => passageBlock(p, article, ctx)).join("\n")}\n</ol>` : nUnread ? `<p>No passage of this revision has a status yet.</p>` : `<p>No formal passages were found in this revision.</p>`}
<p class="articles-data">Data: <a href="/api/articles/article/${int(article.pageid)}">view model (JSON)</a> · <a href="/api/articles/records/${int(article.pageid)}">Literature Links records (JSONL)</a></p>
${user ? `<p class="articles-muted articles-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they audit the locate and challenge runs.</p>` : ""}
</main>`;
  const footer = `<footer class="articles-foot">
  <p>The passages shown are excerpts of ${articleCredit(article)}, shared under ${textLicence(article)}. ${downloadsLine(nText > 0)}</p>
  <details class="articles-about"><summary>Sources, licences, and data</summary>
  ${MAPPING_LICENCE}
  </details>
  <p>${SITE_TOOLS}</p>
</footer>`;
  return shell({
    title: `Articles · ${article.title}`, description, canonical: `https://wikilean.jackmccarthy.org${selfHref}`, main, fullText: nText > 0, footer,
    turnstile: pageNeedsTurnstile(ctx.overlay),
    ...(readerHref ? { readerCss: true, readerJs: true, headerRow: `<div class="atlr-bar">${viewSwitch(readerHref, "cards")}</div>` } : {}),
  });
}

// ---- the article reader ------------------------------------------------------

// The order the passages read in: the article's own (each passage's first mark), any passage
// the revision leaves unmarked after them, in the store's order.
function readingOrder(passages: ArticlesPassage[], html: string): ArticlesPassage[] {
  const at = new Map<string, number>();
  for (const m of html.matchAll(/<mark class="atl" data-p="([0-9a-f]{8,16})" data-status="[a-z_]+" id="p-\1">/g)) {
    if (!at.has(m[1])) at.set(m[1], at.size);
  }
  return passages.map((passage, index) => ({ passage, index, place: at.get(passageHex(passage)) }))
    .sort((a, b) => (a.place ?? Infinity) - (b.place ?? Infinity) || a.index - b.index)
    .map((row) => row.passage);
}

// The panel's index with nothing selected: the passages under the article's section headings,
// in reading order, each row its status, its first words and its lead declaration. A row's
// words wrap and are cut at a word, never in the middle of one (articles-reader.css clamps
// them to two lines), and the row's title carries the whole of what the quote has, for a
// reader who hovers or focuses it.
function readerIndex(passages: ArticlesPassage[], placed: Set<string>, ctx: CardContext): string {
  const groups: { label: string; rows: string[] }[] = [];
  for (const passage of passages) {
    const label = Array.isArray(passage.section) && passage.section.length ? passage.section.join(" › ") : "Lead";
    if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, rows: [] });
    const status = shownStatus(passage, ctx);
    const hex = passageHex(passage);
    const head = leadOf(passage, status, ctx);
    const exact = renderQuote({ exact: String(passage.quote?.exact ?? "") }, quoteCapped(passage)).exact;
    const leadHtml = head
      ? `<code>${text(String(head.loc.declaration ?? "").split(".").pop() ?? "")}</code>${head.more ? ` +${int(head.more)}` : ""}`
      : status === "not_formalized" ? "not found" : "";
    groups[groups.length - 1].rows.push(`<li><a class="atlr-row" href="#p-${hex}" data-p="${hex}" data-status="${status}"` +
      ` title="${attr(plainHead(passage, 220))}">` +
      `<span class="atlr-swatch" data-status="${status}" aria-hidden="true"></span>` +
      `<span class="atlr-row-text"><span class="articles-sr">${text(STATUS[status].label)}: </span>${exact}</span>` +
      `<span class="atlr-row-lead">${leadHtml}${placed.has(hex) ? "" : ` <span class="atlr-row-note">not marked in the text</span>`}</span></a></li>`);
  }
  return `<ol class="atlr-groups">${groups.map((group) =>
    `<li class="atlr-group"><h3 class="atlr-group-h">${text(group.label)}</h3><ol class="atlr-rows">${group.rows.join("")}</ol></li>`).join("")}</ol>`;
}

// The article reader (docs/ARTICLES.md, "Reader"): the pinned revision, readable as the article,
// each passage highlighted by status in its text (the store's marks, build_store --reader-html),
// and a side panel (a bottom sheet on a phone) that lists the passages and, for the one
// selected, shows its status and exactly the card list's cards: Lean code first, the relation,
// the note and the closed Details. The revision's HTML is served as the builder checked it;
// every value around it goes through text() and attr() like the card list's.
export function articlesReaderPage(input: ArticlePageInput & { html: string }): string {
  const { article, user, selfHref, html } = input;
  const parts = articleParts({ ...input, readerHref: selfHref });
  const { ctx, n, counts, key, libraryChips, about, description, overlayRow, agreement } = parts;
  const passages = readingOrder(parts.passages, html);
  const placed = new Set<string>();
  for (const m of html.matchAll(/<mark class="atl" data-p="([0-9a-f]{8,16})"/g)) placed.add(m[1]);
  const nCentral = passages.filter((p) => p.prominence === "subject" || p.prominence === "central").length;
  // The two filters that are not a status or a library, folded away with them.
  const extraChips = chip("central", "The article's own statements", nCentral,
    "Show only the passages that state the article's subject or a named result or definition") +
    `<button type="button" class="articles-chip atlr-dim-chip" data-filter="dim" aria-pressed="false" title="Fade the article text outside the passages">Dim the rest of the text</button>`;
  const headerRow = `<div class="atlr-bar">${viewSwitch(selfHref, "article")}` +
    `<button type="button" class="atlr-panel-toggle" id="atlr-panel-toggle" aria-controls="atlr-panel" aria-expanded="true">Hide the passage list</button></div>`;
  const unplaced = passages.filter((p) => !placed.has(passageHex(p))).length;
  const main = `<div class="atlr-layout" id="articles-reader" data-pageid="${int(article.pageid)}" data-self="${attr(selfHref)}" data-passages="${int(n)}">
<main class="atlr-main" id="atlr-main">
<div class="atlr-head">
${NOTICE_LINE}
<nav class="articles-crumbs" aria-label="Breadcrumb"><a href="/articles">Articles</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="articles-h1">${text(article.title)}</h1>
${counts}
${key}
${filterNote(true)}
${moreFilters(libraryChips, extraChips)}
${about}
<p class="atlr-hint">Select a highlighted passage to see where Lean formalizes it${unplaced ? `. ${plural(unplaced, "passage is", "passages are")} listed in the panel only` : ""}.</p>
</div>
<article class="wl-article-body atlr-article" id="atlr-article" lang="en" aria-label="${attr(article.title)}, Wikipedia revision ${int(article.revision)}">
<div class="atlr-mbar" id="atlr-mbar" aria-hidden="true" hidden></div>
${html}
</article>
</main>
<aside class="atlr-panel" id="atlr-panel" aria-label="Passages and where Lean formalizes them" tabindex="-1">
<div class="atlr-sheet-head"><span class="atlr-handle" aria-hidden="true"></span><button type="button" class="atlr-close" id="atlr-close" aria-label="Close the panel">✕</button></div>
<div class="atlr-nav" id="atlr-nav" hidden><button type="button" class="atlr-back" id="atlr-back">← All passages</button><span class="atlr-step"><button type="button" class="atlr-stepbtn" id="atlr-prev" aria-label="Previous passage">‹</button><span class="atlr-pos" id="atlr-pos" aria-live="polite"></span><button type="button" class="atlr-stepbtn" id="atlr-next" aria-label="Next passage">›</button></span></div>
<div class="atlr-index" id="atlr-index">
<h2 class="articles-h2 atlr-h2" id="atlr-h-index">Passages</h2>
<p class="atlr-missing" id="atlr-missing" role="status" hidden></p>
<nav class="atlr-toc" aria-labelledby="atlr-h-index">${readerIndex(passages, placed, ctx)}</nav>
${overlayRow}${agreement ? `${agreement}\n` : ""}${relationLegend()}
<p class="articles-data">Data: <a href="/api/articles/article/${int(article.pageid)}">view model (JSON)</a> · <a href="/api/articles/records/${int(article.pageid)}">Literature Links records (JSONL)</a> · <a href="${attr(selfHref)}?view=cards">card list</a></p>
${user ? `<p class="articles-muted articles-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they audit the locate and challenge runs.</p>` : ""}
<p class="atlr-keys"><kbd>←</kbd> <kbd>→</kbd> previous and next passage · <kbd>Esc</kbd> back to this list</p>
</div>
<div class="atlr-slot" id="articles-article" data-pageid="${int(article.pageid)}" data-campaign="${attr(article.campaign)}">
${passages.map((p) => passageBlock(p, article, ctx, true)).join("\n")}
</div>
</aside>
</div>
<div class="atlr-peek" id="atlr-peek" aria-hidden="true" hidden></div>
<button type="button" class="atlr-fab" id="atlr-fab" aria-controls="atlr-panel" aria-expanded="false" hidden>${plural(n, "passage", "passages")} <span aria-hidden="true">▴</span></button>`;
  const images = readerImages(article);
  const footer = `<footer class="articles-foot atlr-foot">
  <p>This page is an adaptation of ${articleCredit(article)}, and is shared under ${textLicence(article, true)}, the revision&#x27;s licence. ${downloadsLine(true)}</p>
  <details class="articles-about"><summary>Sources, licences, and adaptations</summary>
  <p>What WikiLean changed: it highlights the revision&#x27;s formal passages by their formalization status, adds a panel of Lean cards beside them with links to the Lean declarations and their source, points the revision&#x27;s links to Wikipedia or, for an article this plane covers, to its page here, removes the Wikimedia logos and any image that is not free, and hides the revision&#x27;s navigation boxes (rows of links to other articles) and any section edit links. Its text is otherwise the revision&#x27;s, its formulas are drawn from their MathML and its images load from Wikimedia. ${images ? "Images and media keep their own licences: each is listed below, linked to its file page, with its author where that page names one, and its licence." : "Images and media keep their own licences, which their file pages on Wikimedia give."}</p>
  ${images ? `${images}\n  ` : ""}${MAPPING_LICENCE}
  </details>
  <p>${SITE_TOOLS}</p>
</footer>`;
  return shell({
    title: `Articles · ${article.title}`, description, canonical: `https://wikilean.jackmccarthy.org${selfHref}`, main,
    readerCss: true, readerJs: true, bodyClass: "atlr-body", headerRow, footer, noticeInMain: true,
    turnstile: pageNeedsTurnstile(ctx.overlay),
    skip: `<a class="atlr-skip" href="#atlr-panel">Skip to the passage panel</a>`,
  });
}

// ---- the dashboard -----------------------------------------------------------

export interface DashboardInput {
  current: ArticlesCurrent;
  universe: ArticlesUniverse | null;
  q: string;
  rows: DashboardRow[];
  matched: number;
  total: number;
  shards: string[];
}

// The dashboard's per-status columns: the statuses a passage can be given (ARTICLE_STATUSES).
function statusCells(row: Record<SummaryStatus, number>): string {
  return ARTICLE_STATUSES.map((s) => `<td class="num" data-status="${s}">${int(row?.[s])}</td>`).join("");
}

function statusHeads(): string {
  return ARTICLE_STATUSES.map((s) => `<th scope="col" class="num"><span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)}</th>`).join("");
}

// A count of passages less those with no status yet (a dashboard row, an importance bucket, the box).
function passagesShown(passages: unknown, unresolved: unknown): number {
  return Math.max(0, count(passages) - count(unresolved));
}

// A title folded for the script's search: the shape articles.js folds a typed query to.
function foldTitle(title: string): string {
  return String(title).normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

// The article list's columns: a name, a bar of its statuses, how many passages were
// checked and how many libraries hold one of its matches — not a spreadsheet of eight
// numbers. Each head carries the key articles.js sorts by; it turns the head into a
// button and sets aria-sort. Without the script they are plain headings over rows the
// server has already put in title order.
const LIST_COLUMNS: ReadonlyArray<{ key: string; label: string; num: boolean; title: string }> = [
  { key: "title", label: "Article", num: false, title: "Sort by title" },
  { key: "formalized", label: "Formalized", num: false, title: "Sort by how much of the article is formalized" },
  { key: "passages", label: "Passages", num: true, title: "Sort by how many passages were checked" },
  { key: "libraries", label: "Libraries", num: true, title: "Sort by how many libraries hold a match" },
];

function listHeads(): string {
  return LIST_COLUMNS.map((col) => `<th scope="col" class="${col.num ? "num " : ""}articles-sortable"` +
    ` data-sort="${col.key}" data-sort-title="${attr(col.title)}">${text(col.label)}</th>`).join("");
}

// One row of that list. Every key the script sorts by is a data attribute, so sorting
// never re-reads a cell, and the library names a row counts are its cell's title.
function listRow(row: DashboardRow, libs: Map<string, LibraryView>): string {
  const byStatus = shownStatusCounts(row as unknown as Partial<Record<SummaryStatus, number>>);
  const shown = passagesShown(row.passages, row.unresolved);
  const ids = list(row.libraries);
  const names = ids.map((id) => libs.get(id)?.info.short ?? id).join(", ");
  return `<tr data-title="${attr(foldTitle(row.title))}" data-passages="${int(shown)}" data-formalized="${int(byStatus.formalized)}"` +
    ` data-libraries="${int(ids.length)}" data-importance="${attr(String(row.importance ?? ""))}">` +
    `<th scope="row"><a href="${attr(row.href)}">${text(row.title)}</a></th>` +
    `<td class="articles-cov">${statusBar(byStatus, "passage", "passages") || `<span class="articles-muted">no status yet</span>`}</td>` +
    `<td class="num">${int(shown)}</td>` +
    `<td class="num">${ids.length ? `<span title="${attr(names)}">${plural(ids.length, "library", "libraries")}</span>`
      : `<span class="articles-muted">none</span>`}</td></tr>`;
}

// The dashboard's "About this build": the build's campaign and date, then each
// record set it holds (record_sets, for a build of several; else record_set).
// built_at defaults to the latest seal's created (build_store), so when it is that
// time the line dates the record sets' export, and a build date only otherwise.
function buildLines(current: ArticlesCurrent): string[] {
  const day = dayOf(String(current.built_at ?? ""));
  const sets = Array.isArray(current.record_sets) && current.record_sets.length
    ? current.record_sets
    : [{ ...current.record_set, campaign: String(current.campaign ?? "") }];
  const exported = Math.max(...sets.map((set) => Date.parse(String(set?.created ?? ""))).filter(Number.isFinite));
  const dated = !day ? "" : Date.parse(String(current.built_at)) === exported
    ? `, from record sets exported ${text(day)}` : `, built ${text(day)}`;
  const lines = [...auditLines(current.audit), `${text(capital(campaignLabel(String(current.campaign ?? ""))))}${dated}.`];
  for (const set of sets) {
    if (!set || typeof set !== "object") continue;
    const seal = { set_id: set.set_id, schema: set.schema, status_rule: set.status_rule, created: set.created, skill_commit: set.skill_commit };
    lines.push(...recordSetLines(String(set.campaign ?? ""), set.set_id ? seal : null, String(set.seal_sha256 ?? "")));
  }
  // The runs' models, operator and dates when the build has them (--provenance).
  const runs = list(current.provenance && typeof current.provenance === "object" ? current.provenance.runs : null)
    .map(asRun).filter((run): run is ArticlesRun => run !== null);
  if (runs.length) lines.push(...runs.map((run) => runLine(run, run.rechecked && typeof run.rechecked === "object" ? run.rechecked : null, "Across the build it")));
  else lines.push(UNRECORDED);
  const supplements = current.supplements && typeof current.supplements === "object" ? current.supplements : null;
  // The text the pages show: of the passages with a status (`shown`), out of those; a store built
  // before `shown` counts every passage given its text, against every passage.
  const pt = supplements?.passage_text && typeof supplements.passage_text === "object" ? supplements.passage_text : null;
  const [withText, of] = !pt ? [null, null]
    : Number.isSafeInteger(pt.shown) ? [count(pt.shown), passagesShown(current.counts?.passages, current.counts?.by_status?.unresolved)]
      : [count(pt.passages), count(current.counts?.passages)];
  lines.push(...supplementLines(supplements, withText, of));
  return lines;
}

export function articlesDashboardPage(input: DashboardInput): string {
  const { current, universe, q, rows, matched, total, shards } = input;
  const counts = current.counts;
  const libs = libraryViews(universe, []);
  const commitOf = (view: LibraryView) => {
    const tree = view.commit ? treeUrl(view.info, view.commit) : null;
    const sha = view.commit ? `<code class="articles-sha">@${text(sha7(view.commit))}</code>` : "";
    return view.commit ? (tree ? `<a href="${attr(tree)}" rel="noopener">${sha}</a>` : sha) : "";
  };
  // The table's libraries: the universe's, and any library the counts name
  // that it does not list (the route passes no universe when its object cannot
  // be read), under its id and with no commit, so the rows come from the counts
  // and the page never says no library holds anything when one does.
  const raw = counts.by_library;
  const byLibraryCounts: ArticlesCurrent["counts"]["by_library"] = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const tableLibs = new Map(libs);
  for (const id of Object.keys(byLibraryCounts)) {
    if (!tableLibs.has(id)) tableLibs.set(id, { id, info: { repo: "", name: id, short: id, blobTemplate: null }, commit: null });
  }
  // Libraries with a passage or a related declaration, most passages first, then
  // most complete locations, then by name; every other library of the universe
  // was searched and holds nothing in this build, so it is listed below the
  // table rather than as a row of zeros.
  const byLibrary = [...tableLibs.values()].map((view) => ({
    view, c: (Object.prototype.hasOwnProperty.call(byLibraryCounts, view.id) ? byLibraryCounts[view.id] : null)
      ?? { articles: 0, passages: 0, locations: 0, related: 0, complete: 0, incomplete: 0 },
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
    ? tableWrap("By library", `<table class="articles-table"><thead><tr><th scope="col">Library</th><th scope="col">Commit</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th><th scope="col" class="num">Complete</th><th scope="col" class="num">Incomplete</th><th scope="col" class="num">Related only</th></tr></thead><tbody>${libraryRows}</tbody></table>`)
    : `<p>No library holds a location or a related declaration in this build.</p>`;
  const emptyLibraries = empty.length
    ? `\n<details id="articles-libs-empty"><summary>${plural(empty.length, held.length ? "more library" : "library", held.length ? "more libraries" : "libraries")} searched, with no locations in this build</summary>` +
      `<ul>${empty.map(({ view }) => `<li>${text(view.info.name)} ${commitOf(view)}</li>`).join("")}</ul></details>`
    : "";
  // A passage with no status is in no count, status or bar here; the totals say how many there are.
  const nUnread = count(counts.by_status?.unresolved);
  const nPassages = passagesShown(counts.passages, nUnread);
  const byStatus = shownStatusCounts(counts.by_status);
  const central = centralFromSummary(counts.by_prominence);
  const centralHtml = central
    ? centralLine(central, nPassages, { object: "an article's subject or a named result or definition", articles: count(counts.articles) })
    : "";
  const centralRow = centralHtml ? `\n${centralHtml}` : "";
  const importance = Object.entries(counts.by_importance ?? {})
    .sort(([a], [b]) => {
      const ia = IMPORTANCE_ORDER.indexOf(a), ib = IMPORTANCE_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a < b ? -1 : a > b ? 1 : 0);
    })
    .map(([key, bucket]) => `<tr><th scope="row">${text(importanceLabel(key))}</th><td class="num">${int(bucket?.articles)}</td><td class="num">${int(passagesShown(bucket?.passages, bucket?.by_status?.unresolved))}</td>${statusCells(bucket?.by_status)}</tr>`)
    .join("");
  const hasImportance = Object.keys(counts.by_importance ?? {}).some((key) => key !== "unknown");
  const list = rows.map((row) => listRow(row, libs)).join("\n");
  const shown = rows.length;
  const showing = q
    ? `${matched} of ${total} articles match “${text(q)}”${matched > shown ? `; showing the first ${shown}` : ""}.`
    : `${total} articles${total > shown ? `; showing the first ${shown} by title. Search to narrow` : ""}.`;
  // id → short name, for rows articles.js builds from the index shards.
  const libraryNames = JSON.stringify(Object.fromEntries([...libs.values()].map((view) => [view.id, view.info.short])));
  const nRelated = count(counts.related);
  const about = aboutFold([
    { h: "Totals", id: "articles-h-totals", body:
      `<p>${plural(count(counts.articles), "article", "articles")} · ${plural(nPassages, "passage", "passages")} · ${plural(count(counts.locations), "Lean location", "Lean locations")}${nRelated ? ` · ${plural(nRelated, "related declaration", "related declarations")}` : ""}</p>\n` +
      `${statusBar(byStatus, "passage", "passages")}\n` +
      `<div class="articles-chips articles-legend">${ARTICLE_STATUSES.map((s) => `<span class="articles-chip is-static" data-status="${s}">${swatch(s)}<span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)} <span class="articles-n">${int(byStatus[s])}</span></span>`).join("")}</div>${centralRow}` },
    { h: "What the colours mean", body: colourLegend() },
    { h: "Which libraries were searched", id: "articles-h-libs", body:
      `<p class="articles-meta">${universe ? `<a href="/articles/universe/${attr(current.universe.sha256)}">${int(current.universe.libraries)} Lean libraries</a>` : `${int(current.universe.libraries)} Lean libraries`}, pinned ${text(current.universe.frozen)} (library set <code>${text(current.universe.id)}</code>)</p>\n` +
      `${libraryTable}${held.length ? `\n<p class="articles-muted">A passage counts toward every library that states it. Complete locations are proved declarations or definitions; incomplete ones are stated without a finished proof (statement only, wanted, or axiom-dependent). “Related only” declarations state something else and are not counted as locations.</p>` : ""}${emptyLibraries}` },
    { h: "How these links were made", body: provList(buildLines(current)) },
    ...(hasImportance ? [{ h: "By WikiProject importance", id: "articles-h-imp", body:
      tableWrap("By WikiProject importance", `<table class="articles-table"><thead><tr><th scope="col">Importance</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th>${statusHeads()}</tr></thead><tbody>${importance}</tbody></table>`) }] : []),
  ], "About this build");
  const main = `<main class="wl-articles" id="articles-dashboard">
<h1 class="articles-h1" id="articles-h-list">Articles</h1>
<p class="articles-lead">Explore mathematical statements and their Lean matches. Choose an article to inspect the evidence, confirm a link, or add a missing declaration. <a href="/contribute">How to contribute</a>.</p>
${countsLine(byStatus, nUnread, [`<b>${plural(count(counts.articles), "article", "articles")}</b>`, plural(nPassages, "passage", "passages")])}
${about}
<section aria-labelledby="articles-h-list">
<form class="articles-search" method="get" action="/articles" role="search"><label for="articles-q">Search covered articles</label> <input id="articles-q" name="q" type="search" value="${attr(q)}" maxlength="200" autocomplete="off"> <button type="submit">Search</button></form>
<p class="articles-count" id="articles-count" role="status">${showing}</p>
${tableWrap("Articles", `<table class="articles-table articles-list" id="articles-list" data-total="${int(total)}" data-shards="${attr(shards.join(" "))}" data-library-names="${attr(libraryNames)}"><thead><tr>${listHeads()}</tr></thead><tbody>
${list}
</tbody></table>`)}
</section>
<p class="articles-data">Data: <a href="/api/articles/current">current build (JSON)</a></p>
</main>`;
  return shell({
    title: "Articles",
    description: `Which WikiProject Mathematics statements are formalized in Lean, and where: ${plural(count(counts.articles), "article", "articles")}, ${plural(nPassages, "passage", "passages")}.`,
    canonical: "https://wikilean.jackmccarthy.org/articles",
    main,
    dashboard: true,
  });
}

// ---- the universe page -------------------------------------------------------

export function articlesUniversePage(universe: ArticlesUniverse): string {
  const libraries = universeLibraries(universe);
  const excludedItems = list(universe.excluded).filter((item) => item !== null && typeof item === "object");
  // Named as every other page names them, so libraries of one repository read apart.
  const infos = libraryInfos(libraries);
  const rows = libraries.map((lib: UniverseLibrary) => {
    const info = infos.get(lib.id) ?? libraryInfo(lib.repo, lib);
    // GitHub repositories only (repoHref); any other host is named, not linked.
    const repo = repoHref(info.repo);
    const tree = treeUrl(info, lib.commit);
    return `<tr><th scope="row">${text(info.name)} <span class="articles-muted">${text(lib.id)}</span></th>` +
      `<td>${repo ? `<a href="${attr(repo)}" rel="noopener">${text(info.repo.replace(/^https:\/\//, ""))}</a>` : text(info.repo)}</td>` +
      `<td>${tree ? `<a href="${attr(tree)}" rel="noopener"><code>${text(lib.commit)}</code></a>` : `<code>${text(lib.commit)}</code>`}</td>` +
      `<td>${text(lib.toolchain ?? "")}</td><td>${text(lib.licence ?? "")}</td></tr>`;
  }).join("");
  const excluded = excludedItems.length
    ? `<h2 class="articles-h2">Excluded</h2><ul class="articles-excluded">${excludedItems.map((item) => `<li><b>${text(item.id)}</b>: ${text(item.reason)}</li>`).join("")}</ul>`
    : "";
  const main = `<main class="wl-articles" id="articles-universe">
<nav class="articles-crumbs" aria-label="Breadcrumb"><a href="/articles">Articles</a> <span aria-hidden="true">›</span> <span>Libraries searched</span></nav>
<h1 class="articles-h1">The libraries searched</h1>
<p class="articles-meta">${int(libraries.length)} Lean libraries, pinned ${text(universe.frozen)} · library set <code>${text(universe.id)}</code> · sha256 <code>${text(universe.sha256)}</code></p>
<p>A “not found” here means exactly this: the passage was searched for in these libraries at these commits and nothing stating it was found. It says nothing about other libraries or later commits.</p>
${tableWrap(`Libraries of set ${universe.id}`, `<table class="articles-table"><thead><tr><th scope="col">Library</th><th scope="col">Repository</th><th scope="col">Commit</th><th scope="col">Toolchain</th><th scope="col">Licence</th></tr></thead><tbody>${rows}</tbody></table>`)}
${excluded}
<p class="articles-data">Data: <a href="/api/articles/universe/${attr(universe.sha256)}">library set (JSON)</a></p>
</main>`;
  return shell({ title: `Articles · libraries searched (${universe.id})`, description: `The Lean libraries and commits searched for the WikiLean formalization reader (library set ${universe.id}).`, main });
}

// ---- the article page's summary box ------------------------------------------

// The box sits under WikiLean's own status badges, which count this page's
// annotations, so it says that its counts are the plane's, for the Wikipedia
// revision the plane checked, and names the page's revision when they differ.
// Its counts are the passages that have a status, as the article's own page counts them: one with
// none yet is in no count and has no status here either, and the box says how many more there are.
export function articlesSummaryBox(entry: IndexEntry, href: string, pageRevision: number | null = null): string {
  const counts = entry as unknown as Record<SummaryStatus, number>;
  const nUnread = count(counts.unresolved);
  const checked = passagesShown(entry.passages, nUnread);
  const parts = ARTICLE_STATUSES.filter((s) => count(counts[s]) > 0)
    .map((s) => `<span class="articles-mini" data-status="${s}"><span class="articles-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(counts[s])} ${text(STATUS[s].plural)}</span>`)
    .join(" ");
  const revision = count(entry.revision);
  const other = pageRevision !== null && Number.isSafeInteger(pageRevision) && pageRevision !== revision
    ? ` <span class="wl-articles-box-rev">This page shows revision ${int(pageRevision)}.</span>`
    : "";
  const found = checked === 0 && nUnread > 0
    ? `WikiLean found ${plural(nUnread, "passage", "passages")} in Wikipedia revision ${int(revision)}, none with a status yet.`
    : `WikiLean checked ${plural(checked, "passage", "passages")} of Wikipedia revision ${int(revision)} against Lean libraries: ${parts || "none yet"}.` +
      "";
  return `<link rel="stylesheet" href="${ARTICLES_CSS}">\n` +
    `<aside class="wl-articles-box" aria-label="Formalization">` +
    `<span class="wl-articles-box-h">Formalization</span> ` +
    `<span class="wl-articles-box-counts">Separately from this page&#x27;s annotations, ${found}${other}</span> ` +
    `<a class="wl-articles-box-link" href="${attr(href)}">See where<span aria-hidden="true"> →</span></a>` +
    `</aside>\n`;
}
