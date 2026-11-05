// Server-rendered HTML for the formalization atlas (docs/ATLAS.md, "UI"):
// the /atlas dashboard, one article's coverage page (the card list), the article
// reader (the pinned revision with its passages highlighted, when the store has it),
// the universe page, the 503/404 message page, and the compact box injected into
// article pages.
//
// Every value from the store is data: text goes through htmlEscape, every
// number through int() (a store object is cast, not validated, so a string
// where a count belongs must never reach the HTML raw), every href is either
// built here from validated parts or parsed and required to be https, and no
// element carries an inline event handler. The only scripts on these pages
// besides the shared theme bootstrap and header search are /assets/atlas.js
// (site/assets/atlas.js) and, on an article with a reader, /assets/atlas-reader.js,
// which write the DOM through textContent, setAttribute and createElement only. The
// reader's revision is the one piece of HTML from the store, served as the builder
// checked it (build_store --reader-html) and only when its sha256 is the view's. Every
// atlas page enforces security.ts atlasCsp, which lets the two inline scripts run by
// their sha256 and no other inline script or handler: change either script and its
// hash there. Status is never colour alone: each status has an icon, a word and its
// own stroke pattern (atlas.css).
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
// line under the header (ATLAS_NOTICE), never as a badge on a card; what an audit
// measured, or that nothing has measured it, is in the page's "About" fold.

import { htmlEscape } from "./engine/html.js";
import { passageTextHtml, quoteFragment, quoteSegments, renderQuote } from "./atlas-math.js";
import { SEARCHBOX_CSS, SEARCHBOX_HTML, SEARCHBOX_SCRIPT } from "./engine/searchbox.js";
import type { AuthUser } from "./auth.js";
import type {
  AtlasArticle, AtlasAudit, AtlasCurrent, AtlasLocation, AtlasPassage, AtlasRun, AtlasSupplements, AtlasUniverse,
  AuditFigure, IndexEntry, PassageContext, ProminenceCounts, RecheckTallies, SummaryStatus,
} from "./atlas.js";
import {
  libraryInfo, libraryInfos, repoHref, sourceUrl, treeUrl, type LibraryEntry, type LibraryInfo, type UniverseLibrary,
} from "./libraries.js";

// Asset versions: bump with any change to site/assets/atlas.{css,js}.
export const ATLAS_CSS = "/assets/atlas.css?v=9";
export const ATLAS_JS = "/assets/atlas.js?v=4";
// The article reader (atlasReaderPage) and the view switch on its card list: bump with any
// change to site/assets/atlas-reader.{css,js}.
export const ATLAS_READER_CSS = "/assets/atlas-reader.css?v=4";
export const ATLAS_READER_JS = "/assets/atlas-reader.js?v=3";
const STYLE_CSS = "/assets/style.css?v=10";

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
  // Never shown: a passage with no status is set aside (ARTICLE_STATUSES, below), so it gets no
  // status word, icon or count anywhere; this entry only completes the record.
  unresolved: { label: "No status yet", icon: "", plural: "with no status yet" },
};
const STATUS_ORDER: SummaryStatus[] = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"];
// The statuses the pages show. A passage with no status ("unresolved" in the store) gets no
// highlight and no status until someone reads it (the maintainer's decision, 2026-11-02, after
// PD5): the article views set it aside (articleParts), and the dashboard and the article box leave
// it out of their counts and their statuses, saying only how many more passages there are
// (unreadSentence). The store's JSON keeps it.
const ARTICLE_STATUSES: SummaryStatus[] = STATUS_ORDER.filter((s) => s !== "unresolved");

// A store's counts by status as the pages show them: the statuses a passage can be given, and
// none for the passages that have none yet.
function shownStatusCounts(raw: Partial<Record<SummaryStatus, number>> | null | undefined): Record<SummaryStatus, number> {
  const counts = { formalized: 0, statement_only: 0, partial: 0, not_formalized: 0, unresolved: 0 };
  for (const s of ARTICLE_STATUSES) counts[s] = count(raw?.[s]);
  return counts;
}

// How many more passages the counts leave out because they have no status yet.
function unreadSentence(n: number, short = false): string {
  if (short) return n === 1 ? "1 more passage has no status yet." : `${int(n)} more passages have no status yet.`;
  return n === 1 ? "1 more passage has no status yet; it is left out until someone reads it."
    : `${int(n)} more passages have no status yet; they are left out until someone reads them.`;
}

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
// after the challenge ran, so one is owed.
export function challengeSentence(challenge: string, runs: CampaignRuns | null): string {
  const run = runs?.challenge ? ` <code>${text(runs.challenge)}</code>` : "";
  switch (challenge) {
    case "survived": return `Re-checked in challenge run${run || " (unnamed)"}: it survived.`;
    case "downgraded": return `Re-checked in challenge run${run || " (unnamed)"}, which downgraded it.`;
    case "not_required": return `Not re-checked: ${run ? `challenge run${run} did not require it` : "no challenge was required"}.`;
    case "sampled_out": return `Not re-checked: sampled out${run ? ` of challenge run${run}` : " of the challenge"}.`;
    case "challenge_pending": return "Not re-checked yet: a challenge is owed (the passage's prominence changed after the challenge ran).";
    default: return `Challenge: ${text(challenge)}.`;
  }
}
const IMPORTANCE_ORDER = ["top", "high", "mid", "low", "bottom", "na", "unknown"];

// Proposal §2.6's table per location, as scripts/atlas/build_store.py ports it
// from formal-companion's cflib.vocab.derive_status (docs/ATLAS.md, check 6),
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

function theoremRow(loc: AtlasLocation, statusRule: number): LocatedStatus {
  if (PART_RELATIONS.has(loc.relation)) return "partial";
  if (loc.statement === true) return "statement_only";
  if (loc.proof_status === "proved") return "formalized";
  if (UNPROVED.has(loc.proof_status)) return "statement_only";
  // Data: D1 makes an exact or close match formalized; a broadMatch stays
  // partial under status rules 1 and 2 and formalizes under 3 (R-BROAD).
  return DATA_MATCH_RELATIONS.has(loc.relation) || statusRule >= STATUS_RULE_BROAD ? "formalized" : "partial";
}

function definitionRow(loc: AtlasLocation, statusRule: number): LocatedStatus {
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
export function locationStatus(kind: string, loc: AtlasLocation, statusRule = 1): LocatedStatus | null {
  if (loc.relation === "relatedMatch") return null;
  const rows: LocatedStatus[] = [];
  if (THEOREM_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(theoremRow(loc, statusRule));
  if (DEFINITION_KINDS.has(kind) || EITHER_KINDS.has(kind)) rows.push(definitionRow(loc, statusRule));
  return rows.length ? rows.reduce((a, b) => (LOCATED_RANK[b] < LOCATED_RANK[a] ? b : a)) : null;
}

// A status outside the vocabulary (a corrupt store) reads as no status, so the passage is set
// aside with the others that have none, rather than throwing; the builder never writes one.
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
function asRun(value: unknown): AtlasRun | null {
  if (!value || typeof value !== "object") return null;
  const run = value as unknown as Record<string, unknown>;
  const fields = ["run_id", "model", "model_name", "reviewer_model", "reviewer_model_name", "operator", "started", "harvested"];
  return fields.every((key) => typeof run[key] === "string" && run[key] !== "") ? (value as AtlasRun) : null;
}

// The runs that made an article's records: its locate run and, when one
// re-checked them, its challenge run, and the inventory run that chose its
// passages when the store names one; null when the store names no models.
interface ArticleRuns { locate: AtlasRun; challenge: AtlasRun | null; inventory: AtlasRun | null }
function articleRuns(article: AtlasArticle): ArticleRuns | null {
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
  return `${text(name)} <span class="atlas-nowrap">(${text(id)})</span>`;
}

// "by Model X (model-x) on 31 Oct–1 Nov 2026 (UTC), locate run loc-… by
// operator": the run's model, its days and the login that ran it.
function runCite(run: AtlasRun, kind: "locate" | "challenge"): string {
  const days = dayRange(run.started, run.harvested);
  return `by ${modelCite(run.model_name, run.model)}${days ? ` on ${text(days)} (UTC)` : ""}, ` +
    `${kind} run <code>${text(run.run_id)}</code> by ${text(run.operator)}`;
}

// challengeSentence's verdicts as a citation of the challenge run that gave
// them: who re-checked the record and when, or which run sampled it out.
// `held` is what a survived verdict says ("it survived", "upheld").
function challengeCitation(challenge: string, run: AtlasRun | null, runs: CampaignRuns | null, held = "it survived"): string {
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
function recheckTallies(passages: AtlasPassage[]): RecheckTallies {
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
function recheckSentence(raw: RecheckTallies, sampling: AtlasRun["sampling"], scope: string): string {
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
function runLine(run: AtlasRun, tallies: RecheckTallies | null = null, scope = "It"): string {
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
function supplementLines(supplements: AtlasSupplements | null | undefined, withText: number | null, passages: number | null): string[] {
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

// The data's audit status, one line under every atlas page's header: a statement about the
// dataset, never a label on a card.
export const ATLAS_NOTICE = "Preliminary: these links have not yet had an independent audit.";

// The notice as the pages place it, under the header.
const NOTICE_LINE = `<p class="atlas-notice" id="atlas-notice">${text(ATLAS_NOTICE)}</p>`;

// What a store without an audit summary says in its "About" fold: a statement about the build,
// which may have been built without one whether or not an audit of its links exists.
export const UNMEASURED = "This build carries no accuracy figure from an audit of its links.";

// An audit as the store cites it, or null: a store object is cast, not validated, so a
// malformed one reads as no audit.
function asAudit(value: unknown): AtlasAudit | null {
  if (!value || typeof value !== "object") return null;
  const audit = value as Record<string, unknown>;
  const strings = ["run_id", "operator", "started", "ended"].every((key) => typeof audit[key] === "string" && audit[key] !== "");
  return strings && Array.isArray(audit.models) && Array.isArray(audit.figures) && count(audit.items) > 0
    ? (value as AtlasAudit) : null;
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
function auditStanding(audit: AtlasAudit): string {
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
  return `<details class="${cls}"><summary>${text(summary)}</summary><ul class="atlas-prov-list">` +
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
// fragment is built from the quote's prose only (atlas-math quoteFragment): the
// rendered article shows neither the layer's MathML tokens nor its TeX. A quote
// with no specific run of prose links to the revision without one.
function inContextUrl(passage: AtlasPassage): string | null {
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

// Whether the card, which shows the quote's suffix, shows where a capped
// head's passage ends: the store says the rest of the passage lies within that
// suffix (build_store.quote_extent). A store built before the flag reads a
// head's passage as going on past its suffix.
function endsInSuffix(passage: Pick<AtlasPassage, "quote" | "passage_ends_in_suffix">): boolean {
  return passage.passage_ends_in_suffix === true && typeof passage.quote?.suffix === "string" && passage.quote.suffix !== "";
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
// prominence (a store built before every record had one).
function centralFromPassages(passages: AtlasPassage[]): CentralCounts | null {
  if (!passages.some((p) => typeof p.prominence === "string")) return null;
  const byStatus = emptyStatusCounts();
  for (const passage of passages) {
    if (CENTRAL_PROMINENCES.includes(passage.prominence as string)) byStatus[passageStatus(passage)] += 1;
  }
  return { byStatus, supporting: passages.filter((p) => p.prominence === "supporting").length };
}

// Between the status counts of the central-statements line: a comma in an
// element of its own, so the summary box's ".atlas-mini + .atlas-mini" margin
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
      .map((s) => `<span class="atlas-mini" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(byStatus[s])} ${text(STATUS[s].plural)}</span>`)
      .join(CENTRAL_SEPARATOR);
    sentence = `${who}${across} ${total === 1 ? "states" : "state"} ${text(scope.object)}${rest}. ` +
      `Of ${total === 1 ? "this one" : `these ${int(total)}`}: ${parts}.`;
  }
  return `<p class="atlas-strip-lead" id="atlas-central"><b>Central statements:</b> ${sentence}</p>`;
}

// ---- shell -----------------------------------------------------------------

// The licences the footers name: the article text's (Wikipedia's, CC BY-SA 4.0) and the
// mapping's (WikiLean's, CC0), which each article's JSON states too (`licence`). Only the
// reader, whose content is the adapted revision, marks its licence rel="license".
const CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/";
const CC_BY_SA = `<a href="${CC_BY_SA_URL}">CC BY-SA 4.0</a>`;
const CC0 = `<a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>`;

// The footer's last paragraph, on every atlas page: the mapping is CC0, apart from the text and
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
function textLicence(article: AtlasArticle, rel = false): string {
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
function readerImages(article: AtlasArticle): string {
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
function articleCredit(article: AtlasArticle): string {
  const history = `https://en.wikipedia.org/w/index.php?curid=${int(article.pageid)}&action=history`;
  return `<a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}">revision ${int(article.revision)}</a> of “${text(article.title)}” on <a href="https://en.wikipedia.org/">Wikipedia</a>, by <a href="${attr(history)}">its contributors</a>`;
}

function shell(opts: {
  title: string; description: string; canonical?: string; main: string; script?: boolean; dashboard?: boolean;
  // The page shows passages in full, so its JSON download carries their text.
  fullText?: boolean;
  // A page that has the article reader (atlasReaderPage, and its card list's view switch):
  // its stylesheet, a class on the body, a skip link before the header, a second header
  // row, its own footer and its script. Absent, the shell is what it always was.
  readerCss?: boolean; bodyClass?: string; skip?: string; headerRow?: string; footer?: string; readerJs?: boolean;
  // The page puts the notice line at the top of its own main column (the reader: under the
  // header still, but beside the sticky panel rather than above it, so the panel keeps the
  // viewport's height).
  noticeInMain?: boolean;
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
<meta name="robots" content="noindex">
<meta name="description" content="${attr(opts.description)}">
${opts.canonical ? `<link rel="canonical" href="${attr(opts.canonical)}">\n` : ""}<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<link rel="stylesheet" href="${STYLE_CSS}">
<link rel="stylesheet" href="${ATLAS_CSS}">
${opts.readerCss ? `<link rel="stylesheet" href="${ATLAS_READER_CSS}">\n` : ""}<style>${SEARCHBOX_CSS}</style>
</head>
<body class="atlas-body${opts.bodyClass ? ` ${attr(opts.bodyClass)}` : ""}">
${opts.skip ? `${opts.skip}\n` : ""}<header class="wl-header">
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
${opts.headerRow ? `${opts.headerRow}\n` : ""}</header>
${opts.noticeInMain ? "" : `${NOTICE_LINE}\n`}${opts.main}
${opts.footer ?? `<footer class="atlas-foot">
  <p>Quoted passages are excerpts of <a href="https://en.wikipedia.org/">Wikipedia</a> articles, available under ${CC_BY_SA}. ${downloadsLine(opts.fullText === true)}</p>
  ${MAPPING_LICENCE}
</footer>`}
${SEARCHBOX_SCRIPT}
${opts.script === false ? "" : `<script src="${ATLAS_JS}"></script>\n`}${opts.readerJs ? `<script src="${ATLAS_READER_JS}"></script>\n` : ""}</body>
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

// The page's libraries by id: the universe's, then any library a location names
// that the universe does not list (the route passes no universe when its object
// cannot be read). Keyed by id, as the store is, so libraries split from one
// repository (Init and Std of Lean core) are two views, named apart (libraryInfos).
function libraryViews(universe: AtlasUniverse | null, extra: Iterable<{ library: string; repo: string; commit: string }>): Map<string, LibraryView> {
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
  return `<div class="atlas-table-wrap" role="region" tabindex="0" aria-label="${attr(`Table: ${label}`)}">${table}</div>`;
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
    return `<span class="atlas-seg" data-status="${s}" style="width:${pct}%" title="${attr(`${counts[s]} ${STATUS[s].plural}`)}"></span>`;
  }).join("");
  return `<div class="atlas-bar" role="img" aria-label="${attr(`${plural(total, one, many)}: ${label}`)}">${segments}</div>`;
}

// The location that justifies a located passage's status: the store's basis
// (build_store derive_status), or, for a store built before it existed, the
// highest-ranked location whose own status (locationStatus) is the passage's.
// Never simply the rank-1 location: rank 1 may state without a proof what
// rank 2 proves.
export function basisLocation(passage: AtlasPassage, status: SummaryStatus, statusRule = 1): AtlasLocation | null {
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
      `<span class="atlas-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span> ` +
      `<span class="atlas-muted">(${text(proofOf(loc).label)})</span>` +
      (states ? `<ul class="atlas-conjuncts" aria-label="Clauses this part states">${states}</ul>` : "") + `</li>`;
  }).join("");
  return `<div class="atlas-coverage" data-coverage-status="${attr(status ?? "none")}">` +
    `<p class="atlas-why">${text(lead)}:</p><ol class="atlas-coverage-parts">${items}</ol></div>`;
}

// The status in words, from the location that gives it. A partial status from an
// exact, close or more general match says why it is not formalized: a definition
// passage is formalized only by a Lean definition, so a proved theorem that states
// it (a characterisation) leaves it partial; and under status rules 1 and 2 a more
// general definition leaves a theorem partial (the section 2.6 table,
// build_store.location_status).
function statusSentence(passage: AtlasPassage, status: SummaryStatus, libs: Map<string, LibraryView>, statusRule = 1): string {
  const meta = STATUS[status];
  const best = basisLocation(passage, status, statusRule);
  if (!best) return `${meta.label}.`;
  const name = libs.get(best.library)?.info.name ?? best.library;
  const states = `${meta.label}: ${name} ${RELATION_PHRASE[best.relation] ?? best.relation}`;
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

interface CardContext {
  libs: Map<string, LibraryView>;
  user: AuthUser | null;
  verdicts: Map<string, string>;
  loginHref: string;
  // The article's campaign token and seal summary, which the Details folds cite.
  campaign: string;
  seal: SealSummary | null;
  statusRule: number;
  // The runs' models, operator and dates when the store has them (--provenance).
  runs: ArticleRuns | null;
  // Where the store's signatures come from, when it has them (--signatures).
  signatures: AtlasSupplements["signatures"] | null;
  // Each passage's full text as HTML (with the sentence around it, when it is only
  // part of one), or null where the page shows the quote.
  texts: Map<AtlasPassage, PassageView | null>;
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
function passageView(passage: AtlasPassage): PassageView | null {
  const t = passage.text;
  if (!t || typeof t !== "object") return null;
  const html = passageTextHtml(t.segments);
  return html === null ? null : { html, before: contextHtml(t.before, "before"), after: contextHtml(t.after, "after") };
}

// A location's provenance, specific and behind a closed fold: the run that found
// it (with its rank and confidence there), the run that re-checked it, the
// export, what the record set does not record, and the keys an audit uses.
function locationDetails(loc: AtlasLocation, ctx: CardContext): string {
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
  return detailsFold("atlas-prov", "Details", lines);
}

// How the declaration relates to the passage, as a badge beside its name (a
// stroke per strength: solid for exact and close, dashed for more general,
// special case and part, dotted for related); the record's own term is its title.
function relationBadge(relation: string): string {
  const word = RELATION_WORD[relation] ?? relation;
  return `<span class="atlas-rel" data-relation="${attr(relation)}" title="${attr(`${capital(RELATION_PHRASE[relation] ?? relation)} (${relation})`)}">${text(word)}</span>`;
}

// The relation words, each beside what it means, under the Passages heading.
function relationLegend(): string {
  const items = RELATION_ORDER.map((rel) => `<li>${relationBadge(rel)} ${text(RELATION_LEGEND[rel])}</li>`).join("");
  return `<div class="atlas-legend-rel"><p class="atlas-muted">How each Lean declaration relates to its passage:</p><ul>${items}</ul></div>`;
}

// The location's signature when the store has one (--signatures), checked to be
// strings; null otherwise, and the card shows the declaration's name alone.
function signatureOf(loc: AtlasLocation): { text: string; full: string | null; deprecatedTo: string | null } | null {
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
  return `<code>${signatureUnits(sig).map((unit) => `<span class="atlas-sig-u">${text(unit)}</span>`).join(" ")}</code>`;
}

// The Lean code block under the declaration's name: its header (binders before
// the colon, no proof), monospace and flowed at the card's width; a header
// shortened to fit keeps the whole type, as Lean prints it, under a fold, and a
// deprecated name says what replaced it.
function signatureBlock(loc: AtlasLocation): string {
  const sig = signatureOf(loc);
  if (!sig) return "";
  return `<pre class="atlas-sig">${sigCode(sig.text)}</pre>\n` +
    (sig.full ? `<details class="atlas-sig-full"><summary>Full type, as Lean prints it</summary><pre class="atlas-sig">${sigCode(sig.full)}</pre></details>\n` : "") +
    (sig.deprecatedTo ? `<p class="atlas-deprecated">Deprecated at this commit, in favour of ${declName(sig.deprecatedTo)}.</p>\n` : "");
}

// A location card leads with the Lean code: the declaration, its signature when
// the store has one (the export names the declaration only; build_store
// --signatures adds the header), then its module and lines. Then the library,
// commit, relation, proof status and the pinned source link; the convention
// note; provenance under a closed Details fold; and the reader's own
// Confirm/Report (never anyone else's).
function locationCard(loc: AtlasLocation, ctx: CardContext): string {
  const view = ctx.libs.get(loc.library);
  const info = view?.info ?? libraryInfo(loc.repo, null);
  const src = sourceUrl(info, loc);
  const proof = proofOf(loc);
  const short = sha7(loc.commit);
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
<p class="atlas-decl-line">${declName(String(loc.declaration ?? ""))} ${relationBadge(loc.relation)}</p>
${signatureBlock(loc)}<p class="atlas-module">${text(loc.module)} · ${text(lines)}</p>
<div class="atlas-card-top">
<span class="atlas-lib" title="${attr(`${info.name} · ${info.repo}`)}">${text(info.short)}</span>
<code class="atlas-commit" title="${attr(`commit ${loc.commit}`)}">@${text(short)}</code>
<span class="atlas-ps${proof.alert ? " is-alert" : ""}" data-proof="${attr(loc.proof_status)}" title="${attr(capital(proof.phrase))}">${proof.alert ? `<span aria-hidden="true">! </span>` : ""}${text(proof.label)}</span>
${src ? `<a class="atlas-src" href="${attr(src)}" rel="noopener" target="_blank">Source @${text(short)}<span aria-hidden="true"> ↗</span><span class="atlas-sr"> (opens ${text(info.name)} at commit ${text(short)}, ${text(lines)})</span></a>` : `<span class="atlas-src is-none">No source link</span>`}
</div>
${loc.note ? `<p class="atlas-note"><span class="atlas-k">${text(NOTE_LABEL[loc.relation] ?? "Note")}:</span> ${text(loc.note)}</p>\n` : ""}${locationDetails(loc, ctx)}
<div class="atlas-card-foot">
${feedback}
</div>
</article>`;
}

function passageStatus(passage: AtlasPassage): SummaryStatus {
  return passage.outcome === "unresolved" ? "unresolved" : asStatus(passage.status);
}

// A passage the article views set aside: one with no status (articleParts).
function setAside(passage: AtlasPassage): boolean {
  return passageStatus(passage) === "unresolved";
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

// The location a passage's peek and index row lead with: the one that gives it its status, else
// its first; `more` counts its other locations (relatedMatch aside).
function leadOf(passage: AtlasPassage, status: SummaryStatus, ctx: CardContext): { loc: AtlasLocation; more: number } | null {
  const strong = list(passage.locations).filter((loc) => loc.relation !== "relatedMatch");
  const loc = basisLocation(passage, status, ctx.statusRule) ?? strong[0] ?? null;
  return loc ? { loc, more: strong.length - 1 } : null;
}

// A passage's first words as plain text, each formula as its TeX: the label a highlight's
// screen-reader name and the peek use. The quote is the passage's head, in the layer's form.
export function plainHead(passage: Pick<AtlasPassage, "quote">, max = 90): string {
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
function readerData(passage: AtlasPassage, status: SummaryStatus, ctx: CardContext): string {
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
function passageBlock(passage: AtlasPassage, article: AtlasArticle, ctx: CardContext, panel = false): string {
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
  if (panel) {
    parts.push(`<section class="atlas-passage atlr-pp${subject ? " is-subject" : ""}" id="atlr-pp-${hex}" data-p="${hex}" data-status="${status}"${prominence} data-libraries="${attr(libraries.join(" "))}" data-item="${attr(passage.item)}"${readerData(passage, status, ctx)} aria-labelledby="atlr-pp-h-${hex}" hidden>`);
  } else {
    parts.push(`<li class="atlas-passage${subject ? " is-subject" : ""}" id="p-${hex}" data-status="${status}"${prominence} data-libraries="${attr(libraries.join(" "))}" data-item="${attr(passage.item)}">`);
  }
  // The heading's pieces are flex items with a visual gap only, and the section's
  // "§ " is CSS generated content, so a visually hidden " · " between them keeps
  // its accessible name from running together ("Theorem · central · § Statement").
  const heading = [
    `<span class="atlas-eyebrow">${text(eyebrow)}</span>`,
    subject ? `<span class="atlas-subject">Subject of the article</span>` : "",
    section ? `<span class="atlas-section">${text(section)}</span>` : "",
  ].filter(Boolean).join(HEADING_SEPARATOR);
  parts.push(`<div class="atlas-p-head"><h3 class="atlas-p-h"${panel ? ` id="atlr-pp-h-${hex}" tabindex="-1"` : ""}>${heading}</h3>${pill(status)}</div>`);
  // A located passage says, in words, which location gives it its status. An
  // absence says so in its own box below (a passage with no status is set aside,
  // articleParts, and never reaches here). When a
  // survived coverage claim gives it its status (D1), the claim's line, with
  // the parts listed, is that sentence; otherwise the line follows it.
  const covering = located ? coverageParts(passage) : [];
  const byClaim = covering.length > 0 && passage.coverage?.basis === true && passage.coverage.status === status;
  if (located && !byClaim) parts.push(`<p class="atlas-why">${text(statusSentence(passage, status, ctx.libs, ctx.statusRule))}</p>`);
  if (covering.length) parts.push(coverageBlock(passage, covering, ctx.libs));
  // The quote verbatim, with its prefix and suffix as context. An ellipsis
  // after it means that the passage continues, and nothing else: it follows a
  // capped head (the passage goes on past the export's 12-word cap), outside
  // the highlight and after any suffix, unless the rest of the passage lies
  // within that suffix. A suffix is context and has no ellipsis of its own.
  // Its formulas render once each, from their TeX (atlas-math renderQuote): the
  // layer holds each twice, as MathML token text and as the TeX annotation.
  // With the store's verified text (--passage-text) the passage is shown in full
  // instead, every formula drawn once from its TeX.
  // A passage that is only part of a sentence is highlighted, the rest of the sentence
  // around it in grey, as a quote's prefix and suffix are.
  const full = ctx.texts.get(passage) ?? null;
  let shownText: string;
  if (full !== null) {
    const inContext = full.before !== "" || full.after !== "";
    shownText = `<blockquote class="atlas-quote atlas-text" data-quote="full"${inContext ? ` data-context=""` : ""}>` +
      (full.before ? `<span class="atlas-affix">${full.before}</span>` : "") +
      (inContext ? `<mark>${full.html}</mark>` : full.html) +
      (full.after ? `<span class="atlas-affix">${full.after}</span>` : "") + `</blockquote>`;
  } else {
    const capped = quoteCapped(passage);
    const shown = renderQuote(quote, capped && !endsInSuffix(passage));
    shownText = `<blockquote class="atlas-quote" data-quote="${capped ? "head" : "whole"}">` +
      (shown.prefix ? `<span class="atlas-affix">…${shown.prefix}</span>` : "") +
      `<mark>${shown.exact}</mark>` +
      (shown.suffix ? `<span class="atlas-affix">${shown.suffix}</span>` : "") +
      (capped && !endsInSuffix(passage) ? QUOTE_CONTINUES : "") +
      `</blockquote>`;
  }
  // In the panel the text is clamped to three lines, with a button the script shows when it is longer.
  parts.push(panel ? `<div class="atlr-clamp">${shownText}</div><button type="button" class="atlr-more" aria-expanded="false" hidden>Show all</button>` : shownText);
  const wikipedia = context ? `<a href="${attr(context)}" rel="noopener" target="_blank">Read it in context on Wikipedia<span aria-hidden="true"> ↗</span></a>` : "";
  if (!panel && ctx.readerHref) {
    parts.push(`<p class="atlas-context"><a href="${attr(ctx.readerHref)}#p-${hex}">Show in article</a>${wikipedia ? ` · ${wikipedia}` : ""}</p>`);
  } else if (!panel && wikipedia) {
    parts.push(`<p class="atlas-context">${wikipedia}</p>`);
  }
  if (strong.length) {
    parts.push(`<div class="atlas-cards">${strong.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
  if (passage.absence) {
    const a = passage.absence;
    parts.push(`<p class="atlas-absence"><span class="atlas-ico" aria-hidden="true">✕</span>` +
      `Searched ${int(a.libraries)} libraries in universe <a href="/atlas/universe/${attr(a.universe_sha256)}">${text(a.universe)}</a> ` +
      `(${text(article.universe.frozen)}) — not found.</p>`);
    const runs = campaignRuns(ctx.campaign);
    const searched = ctx.runs ? `Searched ${runCite(ctx.runs.locate, "locate")}`
      : runs ? `Searched in locate run <code>${text(runs.locate)}</code>` : `Searched in campaign <code>${text(ctx.campaign)}</code>`;
    parts.push(detailsFold("atlas-prov atlas-absence-prov", "Details", [
      `${searched}: ` +
        `the search receipt lists ${int(a.receipt?.queries)} queries and ${int(a.receipt?.candidates)} candidates examined` +
        ` (receipt <code title="${attr(String(a.receipt?.sha256 ?? ""))}">${text(String(a.receipt?.sha256 ?? "").slice(0, 12))}</code>).`,
      ctx.runs ? challengeCitation(String(a.challenge ?? ""), ctx.runs.challenge, runs, "upheld")
        : challengeSentence(String(a.challenge ?? ""), runs),
      ...(ctx.runs ? [] : [UNRECORDED]),
    ]));
  }
  if (related.length && panel) {
    parts.push(`<details class="atlr-related"><summary class="atlas-related-h">Related, but states something else (${int(related.length)})</summary>` +
      `<div class="atlas-cards is-related">${related.map((loc) => locationCard(loc, ctx)).join("\n")}</div></details>`);
  } else if (related.length) {
    parts.push(`<h4 class="atlas-related-h">Related, but states something else</h4>`);
    parts.push(`<div class="atlas-cards is-related">${related.map((loc) => locationCard(loc, ctx)).join("\n")}</div>`);
  }
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
  // The article reader's URL when the store has the article's marked revision (--reader-html):
  // the card list then carries the view switch and a "Show in article" link per passage.
  readerHref?: string | null;
}

// What the card list and the reader share: the card context and the pieces of the page head
// and the coverage strip, each exactly as the card list has always rendered it.
function articleParts(input: ArticlePageInput) {
  const { article, universe, user, verdicts, selfHref } = input;
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
  const byStatus: Record<SummaryStatus, number> = { ...emptyStatusCounts(), unresolved: 0 };
  for (const s of ARTICLE_STATUSES) byStatus[s] = count(summary.by_status?.[s]);
  const assessment = article.assessment;
  const assessed = assessment && (assessment.class || assessment.importance)
    ? `WikiProject Mathematics: ${[assessment.class ? `${assessment.class}-class` : "", assessment.importance ? `${importanceLabel(assessment.importance)} importance` : ""].filter(Boolean).join(", ")}`
    : "";
  const statusChips = ARTICLE_STATUSES.map((s) => chip(`status:${s}`, STATUS[s].label, byStatus[s],
    `Show only ${STATUS[s].plural} passages`, s, STATUS[s].icon)).join("");
  const libraryChips = [...libs.values()]
    .filter((view) => summary.by_library?.[view.id])
    .map((view) => {
      const counts = summary.by_library[view.id];
      const commit = view.commit ? ` at commit ${sha7(view.commit)}` : "";
      const passages = count(counts.passages), locations = count(counts.locations);
      const related = Math.max(0, count(counts.related) - (unreadRelated.get(view.id) ?? 0));
      if (!passages && !related) return "";
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
  const subjects = passages.filter(isSubject).map((passage) => {
    const hex = passageHex(passage);
    const exact = renderQuote({ exact: String(passage.quote?.exact ?? "") }, quoteCapped(passage)).exact;
    const label = `${text(capital(String(passage.kind ?? "")))}: “${exact}”` + (quoteCapped(passage) ? QUOTE_CONTINUES : "");
    return `<li>${hex ? `<a href="#p-${hex}">${label}</a>` : label} ${pill(passageStatus(passage))}</li>`;
  });
  const subjectRow = subjects.length
    ? `<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-subject">Subject</span><ul class="atlas-subject-list" aria-labelledby="atlas-h-subject">${subjects.join("")}</ul></div>\n`
    : "";
  const central = centralFromPassages(passages);
  const centralHtml = central ? centralLine(central, n, { object: "the article's subject or a named result or definition", articles: null }) : "";
  const centralRow = centralHtml ? `${centralHtml}\n` : "";
  const nText = [...texts.values()].filter((view) => view !== null).length;
  const about = detailsFold("atlas-about", "About this data", [...auditLines(article.audit),
    ...recordSetLines(String(article.campaign ?? ""), seal, String(article.record_set ?? ""), runs !== null),
    ...(runs ? [...(runs.inventory ? [runLine(runs.inventory)] : []), runLine(runs.locate),
      ...(runs.challenge ? [runLine(runs.challenge, recheckTallies(passages), "In this article it")] : [])] : [UNRECORDED]),
    ...supplementLines(supplements, nText, n)]);
  const lead = `${plural(n, "passage", "passages")} checked${nLocations ? `, ${plural(nLocations, "Lean location", "Lean locations")}` : ""}${nRelated ? ` (and ${plural(nRelated, "related declaration", "related declarations")})` : ""}.` +
    (nUnread ? ` <span class="atlas-muted" id="atlas-unread">${unreadSentence(nUnread)}</span>` : "");
  const revisionLink = `<a href="${attr(wikipediaRevisionUrl(article.pageid, article.revision))}" rel="noopener">${int(article.revision)}</a>`;
  const description = `Where ${article.title} is formalized in Lean: ${int(summary.by_status?.formalized)} of ${plural(n, "passage", "passages")} formalized.`;
  return {
    libs, seal, statusRule, supplements, runs, texts, ctx, summary, n, assessed, statusChips, libraryChips, searched,
    subjectRow, centralRow, nText, about, lead, revisionLink, description, readerHref, passages, nUnread, byStatus,
  };
}

// The two views of an article with a marked revision, as a segmented control: the article
// reader and the card list (the reader's script carries a selected passage across).
function viewSwitch(readerHref: string, current: "article" | "cards"): string {
  return `<nav class="atlr-switch" aria-label="View">` +
    `<a id="atlr-to-article" href="${attr(readerHref)}"${current === "article" ? ` aria-current="page"` : ""}>Article</a>` +
    `<a id="atlr-to-cards" href="${attr(readerHref)}?view=cards"${current === "cards" ? ` aria-current="page"` : ""}>Card list</a></nav>`;
}

export function atlasArticlePage(input: ArticlePageInput): string {
  const { article, user, wikileanSlug, selfHref } = input;
  const {
    ctx, n, assessed, statusChips, libraryChips, searched, subjectRow, centralRow,
    nText, texts, about, lead, revisionLink, description, readerHref, passages, nUnread, byStatus,
  } = articleParts(input);
  // The quote note is for the passages that show a quote; the text note for those shown in full.
  const anyCapped = passages.some((passage) => texts.get(passage) === null && quoteCapped(passage));
  const nContext = [...texts.values()].filter((view) => view !== null && (view.before !== "" || view.after !== "")).length;
  const textNote = nText
    ? `<p class="atlas-muted" id="atlas-text-note">${nText === n ? "Each passage is" : `${plural(nText, "passage is", "passages are")}`} shown in full, as revision ${int(article.revision)} states it (checked against its record&#x27;s span hash when the store was built).` +
      (nContext ? ` Where a passage is only part of a sentence, it is highlighted and the rest of the sentence is shown in grey around it, from the same copy of the revision; a list item also shows the sentence that introduces it.` : "") +
      ` Formulas are drawn from their TeX; hover one to see it.</p>\n`
    : "";
  const anySignature = passages.some((passage) => list(passage.locations).some((loc) => signatureOf(loc) !== null));
  const sigNote = anySignature && ctx.signatures
    ? `<p class="atlas-muted" id="atlas-sig-note">Each card&#x27;s Lean code is its declaration&#x27;s type as Lean prints it, from universe ${text(String(ctx.signatures.universe ?? ""))}&#x27;s declaration corpus at the card&#x27;s commit, laid out as a header: binders before the colon, then the hypotheses as arrows (the corpus keeps no hypothesis names) and the conclusion, in Mathlib&#x27;s notation (𝓝, π, R[X], Type*). It is not the source text; Source opens that.</p>\n`
    : "";
  const main = `<main class="wl-atlas" id="atlas-article" data-pageid="${int(article.pageid)}" data-campaign="${attr(article.campaign)}">
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="atlas-h1">${text(article.title)}</h1>
<p class="atlas-meta">Wikipedia revision ${revisionLink}${assessed ? ` · ${text(assessed)}` : ""}${wikileanSlug ? ` · <a href="/${attr(encodeURIComponent(wikileanSlug))}">Annotated article on WikiLean</a>` : ""}</p>
<p class="atlas-meta">${searched}</p>
${about}
<section class="atlas-strip" aria-labelledby="atlas-h-coverage">
<h2 class="atlas-h2" id="atlas-h-coverage">Coverage</h2>
<p class="atlas-strip-lead">${lead}</p>
${statusBar(byStatus, "passage", "passages")}
${subjectRow}<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-status">Status</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-status">${statusChips}</div></div>
${centralRow}${libraryChips ? `<div class="atlas-strip-row"><span class="atlas-strip-h" id="atlas-h-where">Where</span><div class="atlas-chips" role="group" aria-labelledby="atlas-h-where">${libraryChips}</div></div>` : ""}
<p class="atlas-filter-note" role="status"><span class="atlas-filter-text"></span> <button type="button" class="atlas-clear" hidden>Show all passages</button></p>
</section>
<h2 class="atlas-h2" id="atlas-h-passages">Passages</h2>
${textNote}${sigNote}${anyCapped ? `<p class="atlas-muted" id="atlas-quote-note">Quotes show at most the first ${QUOTE_MAX_WORDS} words of a passage (the record format's quotation cap; a formula's MathML text counts toward it). A … after the highlighted quote means the passage continues; “Read it in context on Wikipedia” shows the rest. Hover a formula for its TeX.</p>\n` : ""}${passages.length ? `${relationLegend()}\n` : ""}${passages.length ? `<ol class="atlas-passages" aria-labelledby="atlas-h-passages">\n${passages.map((p) => passageBlock(p, article, ctx)).join("\n")}\n</ol>` : nUnread ? `<p>No passage of this revision has a status yet.</p>` : `<p>No formal passages were found in this revision.</p>`}
<p class="atlas-data">Data: <a href="/api/atlas/article/${int(article.pageid)}">view model (JSON)</a> · <a href="/api/atlas/records/${int(article.pageid)}">Literature Links records (JSONL)</a></p>
${user ? `<p class="atlas-muted atlas-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they audit the locate and challenge runs.</p>` : ""}
</main>`;
  const footer = `<footer class="atlas-foot">
  <p>The passages shown are excerpts of ${articleCredit(article)}, shared under ${textLicence(article)}. ${downloadsLine(nText > 0)}</p>
  ${MAPPING_LICENCE}
</footer>`;
  return shell({
    title: `Atlas · ${article.title}`, description, canonical: `https://wikilean.jackmccarthy.org${selfHref}`, main, fullText: nText > 0, footer,
    ...(readerHref ? { readerCss: true, readerJs: true, headerRow: `<div class="atlr-bar">${viewSwitch(readerHref, "cards")}</div>` } : {}),
  });
}

// ---- the article reader ------------------------------------------------------

// What each status means, beside a sample of its highlight, in the reader's legend.
const READER_LEGEND: Record<SummaryStatus, string> = {
  formalized: "a Lean declaration states it, proved, or a Lean definition defines it",
  statement_only: "Lean states it, but the proof is not finished",
  partial: "Lean states part of it, or a more general or special form",
  not_formalized: "searched every library of the universe and not found",
  unresolved: "the locate run did not settle it; not counted as not formalized",
};

// The order the passages read in: the article's own (each passage's first mark), any passage
// the revision leaves unmarked after them, in the store's order.
function readingOrder(passages: AtlasPassage[], html: string): AtlasPassage[] {
  const at = new Map<string, number>();
  for (const m of html.matchAll(/<mark class="atl" data-p="([0-9a-f]{8,16})" data-status="[a-z_]+" id="p-\1">/g)) {
    if (!at.has(m[1])) at.set(m[1], at.size);
  }
  return passages.map((passage, index) => ({ passage, index, place: at.get(passageHex(passage)) }))
    .sort((a, b) => (a.place ?? Infinity) - (b.place ?? Infinity) || a.index - b.index)
    .map((row) => row.passage);
}

// The panel's index with nothing selected: the passages under the article's section headings,
// in reading order, each row its status, its first words and its lead declaration.
function readerIndex(passages: AtlasPassage[], placed: Set<string>, ctx: CardContext): string {
  const groups: { label: string; rows: string[] }[] = [];
  for (const passage of passages) {
    const label = Array.isArray(passage.section) && passage.section.length ? passage.section.join(" › ") : "Lead";
    if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, rows: [] });
    const status = passageStatus(passage);
    const hex = passageHex(passage);
    const head = leadOf(passage, status, ctx);
    const exact = renderQuote({ exact: String(passage.quote?.exact ?? "") }, quoteCapped(passage)).exact;
    const leadHtml = head
      ? `<code>${text(String(head.loc.declaration ?? "").split(".").pop() ?? "")}</code>${head.more ? ` +${int(head.more)}` : ""}`
      : status === "not_formalized" ? "not found" : "";
    groups[groups.length - 1].rows.push(`<li><a class="atlr-row" href="#p-${hex}" data-p="${hex}" data-status="${status}">` +
      `<span class="atlr-swatch" data-status="${status}" aria-hidden="true"></span>` +
      `<span class="atlr-row-text"><span class="atlas-sr">${text(STATUS[status].label)}: </span>${exact}</span>` +
      `<span class="atlr-row-lead">${leadHtml}${placed.has(hex) ? "" : ` <span class="atlr-row-note">not marked in the text</span>`}</span></a></li>`);
  }
  return `<ol class="atlr-groups">${groups.map((group) =>
    `<li class="atlr-group"><h3 class="atlr-group-h">${text(group.label)}</h3><ol class="atlr-rows">${group.rows.join("")}</ol></li>`).join("")}</ol>`;
}

// The article reader (docs/ATLAS.md, "Reader"): the pinned revision, readable as the article,
// each passage highlighted by status in its text (the store's marks, build_store --reader-html),
// and a side panel (a bottom sheet on a phone) that lists the passages and, for the one
// selected, shows its status and exactly the card list's cards: Lean code first, the relation,
// the note and the closed Details. The revision's HTML is served as the builder checked it;
// every value around it goes through text() and attr() like the card list's.
export function atlasReaderPage(input: ArticlePageInput & { html: string }): string {
  const { article, user, wikileanSlug, selfHref, html } = input;
  const parts = articleParts({ ...input, readerHref: selfHref });
  const { ctx, n, assessed, statusChips, libraryChips, searched, subjectRow, centralRow, about, lead, revisionLink, description, byStatus } = parts;
  const passages = readingOrder(parts.passages, html);
  const placed = new Set<string>();
  for (const m of html.matchAll(/<mark class="atl" data-p="([0-9a-f]{8,16})"/g)) placed.add(m[1]);
  const nCentral = passages.filter((p) => p.prominence === "subject" || p.prominence === "central").length;
  const centralChip = chip("central", "Central only", nCentral, "Show only the subject and central passages (the article's own statements)");
  const headerRow = `<div class="atlr-bar">${viewSwitch(selfHref, "article")}` +
    `<div class="atlr-chips" role="group" aria-label="Show passages">${statusChips}${centralChip}${libraryChips}` +
    `<button type="button" class="atlas-chip atlr-dim-chip" data-filter="dim" aria-pressed="false" title="Fade the article text outside the passages">Dim other text</button></div></div>`;
  const legend = `<ul class="atlr-legend">${ARTICLE_STATUSES.map((s) =>
    `<li><span class="atlr-sample" data-status="${s}"><span aria-hidden="true">${STATUS[s].icon} </span>${text(STATUS[s].label)}</span> ${text(READER_LEGEND[s])}</li>`).join("")}</ul>`;
  const unplaced = passages.filter((p) => !placed.has(passageHex(p))).length;
  const main = `<div class="atlr-layout" id="atlas-reader" data-pageid="${int(article.pageid)}" data-self="${attr(selfHref)}" data-passages="${int(n)}">
<main class="atlr-main" id="atlr-main">
<div class="atlr-head">
${NOTICE_LINE}
<nav class="atlas-crumbs" aria-label="Breadcrumb"><a href="/atlas">Formalization atlas</a> <span aria-hidden="true">›</span> <span>${text(article.title)}</span></nav>
<h1 class="atlas-h1">${text(article.title)}</h1>
<p class="atlas-meta">Wikipedia revision ${revisionLink} (pinned)${assessed ? ` · ${text(assessed)}` : ""}${wikileanSlug ? ` · <a href="/${attr(encodeURIComponent(wikileanSlug))}">Annotated article on WikiLean</a>` : ""}</p>
<p class="atlas-meta">${searched}</p>
<p class="atlr-hint">The ${plural(n, "highlighted passage is the formal statement", "highlighted passages are the formal statements")} the atlas checked against ${int(article.universe.libraries)} Lean libraries${unplaced ? ` (${plural(unplaced, "passage is", "passages are")} listed in the panel only)` : ""}. Select one to see where it is formalized; the <a href="#atlr-legend">legend</a> says what each highlight means.</p>
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
<h2 class="atlas-h2 atlr-h2" id="atlr-h-index">Passages</h2>
<p class="atlr-missing" id="atlr-missing" role="status" hidden></p>
<p class="atlas-strip-lead">${lead}</p>
${statusBar(byStatus, "passage", "passages")}
${subjectRow}${centralRow}<p class="atlr-filter-note" role="status"><span id="atlr-filter-text"></span> <button type="button" class="atlas-clear" id="atlr-clear" hidden>Show all passages</button></p>
<nav class="atlr-toc" aria-labelledby="atlr-h-index">${readerIndex(passages, placed, ctx)}</nav>
<h3 class="atlr-h3" id="atlr-legend">Legend</h3>
${legend}
${relationLegend()}
${about}
<p class="atlas-data">Data: <a href="/api/atlas/article/${int(article.pageid)}">view model (JSON)</a> · <a href="/api/atlas/records/${int(article.pageid)}">Literature Links records (JSONL)</a> · <a href="${attr(selfHref)}?view=cards">card list</a></p>
${user ? `<p class="atlas-muted atlas-fb-help">Confirm says the link is right as stated (library, declaration and relation); Report says it is wrong. A report can carry a short note, which the WikiLean maintainers read when they audit the locate and challenge runs.</p>` : ""}
<p class="atlr-keys"><kbd>←</kbd> <kbd>→</kbd> previous and next passage · <kbd>Esc</kbd> back to this list</p>
</div>
<div class="atlr-slot" id="atlas-article" data-pageid="${int(article.pageid)}" data-campaign="${attr(article.campaign)}">
${passages.map((p) => passageBlock(p, article, ctx, true)).join("\n")}
</div>
</aside>
</div>
<div class="atlr-peek" id="atlr-peek" aria-hidden="true" hidden></div>
<button type="button" class="atlr-fab" id="atlr-fab" aria-controls="atlr-panel" aria-expanded="false" hidden>${plural(n, "passage", "passages")} <span aria-hidden="true">▴</span></button>`;
  const images = readerImages(article);
  const footer = `<footer class="atlas-foot atlr-foot">
  <p>This page is an adaptation of ${articleCredit(article)}, and is shared under ${textLicence(article, true)}, the revision&#x27;s licence. ${downloadsLine(true)}</p>
  <p>What the atlas changed: it highlights the revision&#x27;s formal passages by their formalization status, adds a panel of Lean cards beside them with links to the Lean declarations and their source, points the revision&#x27;s links to Wikipedia or, for an article in the atlas, to its page here, removes the Wikimedia logos and any image that is not free, and hides the revision&#x27;s navigation boxes (rows of links to other articles) and any section edit links. Its text is otherwise the revision&#x27;s, its formulas are drawn from their MathML and its images load from Wikimedia. ${images ? "Images and media keep their own licences: each is listed below, linked to its file page, with its author where that page names one, and its licence." : "Images and media keep their own licences, which their file pages on Wikimedia give."}</p>
  ${images ? `${images}\n  ` : ""}${MAPPING_LICENCE}
</footer>`;
  return shell({
    title: `Atlas · ${article.title}`, description, canonical: `https://wikilean.jackmccarthy.org${selfHref}`, main,
    readerCss: true, readerJs: true, bodyClass: "atlr-body", headerRow, footer, noticeInMain: true,
    skip: `<a class="atlr-skip" href="#atlr-panel">Skip to the passage panel</a>`,
  });
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

// The dashboard's per-status columns: the statuses a passage can be given (ARTICLE_STATUSES).
function statusCells(row: Record<SummaryStatus, number>): string {
  return ARTICLE_STATUSES.map((s) => `<td class="num" data-status="${s}">${int(row?.[s])}</td>`).join("");
}

function statusHeads(): string {
  return ARTICLE_STATUSES.map((s) => `<th scope="col" class="num"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)}</th>`).join("");
}

// A count of passages less those with no status yet (a dashboard row, an importance bucket, the box).
function passagesShown(passages: unknown, unresolved: unknown): number {
  return Math.max(0, count(passages) - count(unresolved));
}

function libraryTags(ids: string[], libs: Map<string, LibraryView>): string {
  if (!ids.length) return `<span class="atlas-muted">none</span>`;
  return ids.map((id) => {
    const view = libs.get(id);
    return `<span class="atlas-libtag" title="${attr(view ? `${view.info.name} · ${view.info.repo}` : id)}">${text(view?.info.short ?? id)}</span>`;
  }).join(" ");
}

// The dashboard's "About this build": the build's campaign and date, then each
// record set it holds (record_sets, for a build of several; else record_set).
// built_at defaults to the latest seal's created (build_store), so when it is that
// time the line dates the record sets' export, and a build date only otherwise.
function buildLines(current: AtlasCurrent): string[] {
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
    .map(asRun).filter((run): run is AtlasRun => run !== null);
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

export function atlasDashboardPage(input: DashboardInput): string {
  const { current, universe, q, rows, matched, total, shards } = input;
  const counts = current.counts;
  const libs = libraryViews(universe, []);
  const commitOf = (view: LibraryView) => {
    const tree = view.commit ? treeUrl(view.info, view.commit) : null;
    const sha = view.commit ? `<code class="atlas-sha">@${text(sha7(view.commit))}</code>` : "";
    return view.commit ? (tree ? `<a href="${attr(tree)}" rel="noopener">${sha}</a>` : sha) : "";
  };
  // The table's libraries: the universe's, and any library the counts name
  // that it does not list (the route passes no universe when its object cannot
  // be read), under its id and with no commit, so the rows come from the counts
  // and the page never says no library holds anything when one does.
  const raw = counts.by_library;
  const byLibraryCounts: AtlasCurrent["counts"]["by_library"] = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
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
    ? tableWrap("By library", `<table class="atlas-table"><thead><tr><th scope="col">Library</th><th scope="col">Commit</th><th scope="col" class="num">Articles</th><th scope="col" class="num">Passages</th><th scope="col" class="num">Complete</th><th scope="col" class="num">Incomplete</th><th scope="col" class="num">Related only</th></tr></thead><tbody>${libraryRows}</tbody></table>`)
    : `<p>No library holds a location or a related declaration in this build.</p>`;
  const emptyLibraries = empty.length
    ? `\n<details id="atlas-libs-empty"><summary>${plural(empty.length, held.length ? "more library" : "library", held.length ? "more libraries" : "libraries")} searched, with no locations in this build</summary>` +
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
  const list = rows.map((row) => `<tr data-title="${attr(row.title.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase())}"><th scope="row"><a href="${attr(row.href)}">${text(row.title)}</a></th><td class="num">${int(passagesShown(row.passages, row.unresolved))}</td>${statusCells(row as unknown as Record<SummaryStatus, number>)}<td>${libraryTags(row.libraries ?? [], libs)}</td><td>${text(importanceLabel(row.importance))}</td></tr>`).join("\n");
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
<p class="atlas-meta">${universe ? `<a href="/atlas/universe/${attr(current.universe.sha256)}">Universe ${text(current.universe.id)}</a>` : `Universe ${text(current.universe.id)}`}: ${int(current.universe.libraries)} libraries, frozen ${text(current.universe.frozen)}</p>
${detailsFold("atlas-about", "About this build", buildLines(current))}
<section class="atlas-strip" aria-labelledby="atlas-h-totals">
<h2 class="atlas-h2" id="atlas-h-totals">Totals</h2>
<p class="atlas-strip-lead">${plural(count(counts.articles), "article", "articles")} · ${plural(nPassages, "passage", "passages")} · ${plural(count(counts.locations), "Lean location", "Lean locations")}${nRelated ? ` · ${plural(nRelated, "related declaration", "related declarations")}` : ""}${nUnread ? ` <span class="atlas-muted" id="atlas-unread">${unreadSentence(nUnread)}</span>` : ""}</p>
${statusBar(byStatus, "passage", "passages")}
<div class="atlas-chips atlas-legend">${ARTICLE_STATUSES.map((s) => `<span class="atlas-chip is-static" data-status="${s}">${swatch(s)}<span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${text(STATUS[s].label)} <span class="atlas-n">${int(byStatus[s])}</span></span>`).join("")}</div>${centralRow}
</section>
<section aria-labelledby="atlas-h-libs">
<h2 class="atlas-h2" id="atlas-h-libs">By library</h2>
${libraryTable}${held.length ? `\n<p class="atlas-muted">A passage counts toward every library that states it. Complete locations are proved declarations or definitions; incomplete ones are stated without a finished proof (statement only, wanted, or axiom-dependent). “Related only” declarations state something else and are not counted as locations.</p>` : ""}${emptyLibraries}
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
<p class="atlas-data">Data: <a href="/api/atlas/current">current build (JSON)</a></p>
</main>`;
  return shell({
    title: "Formalization atlas",
    description: `Which WikiProject Mathematics statements are formalized in Lean, and where: ${plural(count(counts.articles), "article", "articles")}, ${plural(nPassages, "passage", "passages")}.`,
    canonical: "https://wikilean.jackmccarthy.org/atlas",
    main,
    dashboard: true,
  });
}

// ---- the universe page -------------------------------------------------------

export function atlasUniversePage(universe: AtlasUniverse): string {
  const libraries = universeLibraries(universe);
  const excludedItems = list(universe.excluded).filter((item) => item !== null && typeof item === "object");
  // Named as every other page names them, so libraries of one repository read apart.
  const infos = libraryInfos(libraries);
  const rows = libraries.map((lib: UniverseLibrary) => {
    const info = infos.get(lib.id) ?? libraryInfo(lib.repo, lib);
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
// Its counts are the passages that have a status, as the article's own page counts them: one with
// none yet is in no count and has no status here either, and the box says how many more there are.
export function atlasSummaryBox(entry: IndexEntry, href: string, pageRevision: number | null = null): string {
  const counts = entry as unknown as Record<SummaryStatus, number>;
  const nUnread = count(counts.unresolved);
  const checked = passagesShown(entry.passages, nUnread);
  const parts = ARTICLE_STATUSES.filter((s) => count(counts[s]) > 0)
    .map((s) => `<span class="atlas-mini" data-status="${s}"><span class="atlas-ico" aria-hidden="true">${STATUS[s].icon}</span>${int(counts[s])} ${text(STATUS[s].plural)}</span>`)
    .join(" ");
  const revision = count(entry.revision);
  const other = pageRevision !== null && Number.isSafeInteger(pageRevision) && pageRevision !== revision
    ? ` <span class="wl-atlas-box-rev">This page shows revision ${int(pageRevision)}.</span>`
    : "";
  const found = checked === 0 && nUnread > 0
    ? `the atlas found ${plural(nUnread, "passage", "passages")} in Wikipedia revision ${int(revision)}, none with a status yet.`
    : `the atlas checked ${plural(checked, "passage", "passages")} of Wikipedia revision ${int(revision)} against Lean libraries: ${parts || "none yet"}.` +
      (nUnread ? ` ${unreadSentence(nUnread, true)}` : "");
  return `<link rel="stylesheet" href="${ATLAS_CSS}">\n` +
    `<aside class="wl-atlas-box" aria-label="Formalization atlas">` +
    `<span class="wl-atlas-box-h">Formalization atlas</span> ` +
    `<span class="wl-atlas-box-counts">Separately from this page&#x27;s annotations, ${found}${other}</span> ` +
    `<a class="wl-atlas-box-link" href="${attr(href)}">See where<span aria-hidden="true"> →</span></a>` +
    `</aside>\n`;
}
