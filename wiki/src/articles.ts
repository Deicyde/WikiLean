// The articles plane: a separate, read-only data plane that shows which
// WikiProject Mathematics passages are formalized in Lean, in which library, at
// which commit (design: docs/ARTICLES.md). The store is a static tree that
// scripts/articles/build_store.py builds from sealed locate record sets; the
// Worker reads it through the optional R2 binding ARTICLES (wrangler.jsonc binds
// it to the private bucket wikilean-articles) and never writes it.
//
// What this plane deliberately does not do: no D1 annotation writes, no item
// identity matching, no 256 KB annotation cap, no EDIT_LIMITER on reads, and no
// interaction with the nightly moderation pipeline. Its only D1 surface is the
// small append-only atlas_feedback table (migration 0015), where signed-in people
// confirm, report or withdraw on single locations; the pipeline bearer exports
// those rows as benchmark seeds.
//
// Unbound ARTICLES → every articles page and API route answers 503 "articles store not
// configured" (the feedback export, which reads only D1, keeps working). Bound
// without current.json, the store's publish switch → the same routes answer 503
// "articles store has no published build" and article pages show no box, whatever
// else the bucket holds.

import type { Context, Hono } from "hono";
import type { ContentfulStatusCode } from "hono/utils/http-status";
import { getUser, requireRole, type AuthUser } from "./auth.js";
import { editLimitKey } from "./agents.js";
import type { Env } from "./env.js";
import type { UniverseLibrary } from "./libraries.js";
import type { PassageSegment } from "./articles-math.js";
import { articlesCsp, checkOrigin, limiterIp, MAX_JSON_BODY_BYTES, readJsonCapped, researchExportSalt } from "./security.js";
import { MAX_FLAG_COMMENT_LEN } from "./validation.js";
import {
  accountFor, CC0_AGREEMENT_TEXT, CC0_AGREEMENT_VERSION, loadAgreement, loadOverlay, loadOverlayTags, loadTag,
  MAX_DECLARATION_CHARS, MAX_REMOVE_REASON_CHARS, MAX_TAG_NOTE_CHARS, mergeOverlay, overlayTableMissing, pairKey,
  statusRuleOf, tagAllowance, tagLimitKey, TAG_RELATIONS, validDeclaration, validRelation, verifyTurnstile,
  writeBrake, type ArticleTag, type TagActor, type TagAllowance,
} from "./articles-overlay.js";
import {
  articlesArticlePage,
  articlesDashboardPage,
  articlesMessagePage,
  articlesReaderPage,
  articlesUniversePage,
  pageNeedsTurnstile,
  type DashboardRow,
} from "./articles-render.js";

type Ctx = Context<{ Bindings: Env }>;

// ---- store contract (mirrors scripts/articles/build_store.py) -----------------

export const ARTICLES_PREFIX = "articles/v1/";
export const ARTICLE_SCHEMA = "wikilean.articles.article/1";
export const CURRENT_SCHEMA = "wikilean.articles.current/1";
export const UNIVERSE_SCHEMA = "wikilean.articles.universe/1";
export const INDEX_SCHEMA = "wikilean.articles.index/1";
export const SUMMARY_STATUSES = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"] as const;
export type SummaryStatus = (typeof SUMMARY_STATUSES)[number];
const MAX_INDEX_SHARDS = 256;

export interface ArticlesLocation {
  library: string;
  repo: string;
  commit: string;
  declaration: string;
  module: string;
  // Relative to the repository root.
  path: string;
  line: number;
  // Optional: absent when the library index records only the first line (as
  // atlas-lean's published index does); the page then says "line N".
  end_line?: number;
  relation: string;
  proof_status: string;
  // The library index's flag for a Prop-valued definition (proposal §2.4): it
  // states a theorem without proving it. Absent from stores built before it.
  statement?: boolean;
  rank: number;
  confidence: string;
  challenge: string;
  location_key: string;
  // The producer's revision- and commit-free key for the judgment: a store
  // built from a cf-locate-recordset/2 set carries it (build_store), and
  // feedback rows copy it; a /1 store has none, so its rows hold NULL.
  pair_key?: string | null;
  note: string | null;
  // Optional (build_store --signatures): the declaration's Lean header, from the
  // universe's declaration corpus at this commit. `elided` marks a header that
  // was shortened to fit, which then keeps the whole type in `type`;
  // `deprecated_to` names what the library deprecated it in favour of. Absent
  // from a store built without the side file: the card then shows the name only.
  signature?: { text: string; elided: boolean; type?: string; deprecated_to?: string } | null;
}

// One run as a store's provenance cites it (build_store --provenance): its
// model (the worker's) and reviewer model, the login that ran it, and its
// window, from the run's start to its harvest (ISO 8601 UTC).
export interface ArticlesRun {
  run_id: string;
  mode: string;
  model: string;
  model_name: string;
  reviewer_model: string;
  reviewer_model_name: string;
  operator: string;
  started: string;
  harvested: string;
  skill_commit: string;
  sampling?: { seed?: number; sample_rate?: number; strong_sample_rate?: number; absence_sample_rate?: number };
  // A challenge run in current.json: what it re-checked across the build, by the
  // passages' prominence (build_store recheck_tallies).
  rechecked?: RecheckTallies;
}

// [re-checked, of] per kind of record, for central (subject or central) passages and
// supporting ones.
export interface RecheckTallies {
  central: { locations: [number, number]; absences: [number, number] };
  supporting: { locations: [number, number]; absences: [number, number] };
}

// The side files a store merged (build_store apply_supplements), by kind.
export interface ArticlesSupplements {
  // `passages` got their full text; `shown` is how many of those have a status, the ones a page
  // shows (absent from stores built before it).
  passage_text?: { schema: string; sha256: string; passages?: number; shown?: number };
  signatures?: {
    schema: string; sha256: string; corpus_sha256: string; corpus_built: string; universe: string;
    locations?: number; declarations?: number;
  };
  provenance?: { schema: string; sha256: string };
  reader_html?: { schema: string; sha256: string; articles?: number; passages?: number; marks?: number };
  audit_summary?: { schema: string; sha256: string };
}

// Optional (build_store --audit-summary): what an audit of the build's links measured, in
// every article and in current.json. The run that judged a sample of `items` items, whether it
// was blind and whether it was independent of the runs it judged, and each figure as an
// estimate: the share judged right with its 95% interval, from `drawn` items standing for
// `population`, weighed by `weights` (null: unweighted) to an effective sample of `n_eff`.
export interface ArticlesAudit {
  run_id: string;
  operator: string;
  started: string;
  ended: string;
  models: Array<{ model: string; model_name: string }>;
  blind: boolean;
  independent: boolean;
  items: number;
  figures: AuditFigure[];
}

export interface AuditFigure {
  id: string;
  label: string;
  share: number;
  ci95: [number, number];
  weights: string | null;
  n_eff: number;
  drawn: number;
  population: number;
}

// Optional (build_store --reader-html): the article's pinned revision, sanitised, with
// every placed passage's text wrapped in <mark class="atl" data-p="<item hex>"
// data-status="<status>">, stored as reader/<pageid>.html. The builder checked that its
// text is the revision's layer and each passage's marks its span; `sha256` is of the
// file's UTF-8 bytes, which the page checks before it serves them. `unplaced` lists the
// passage ids the file leaves unmarked, and `images` the files it shows (Wikimedia Commons or
// English Wikipedia uploads), each with its licence's name, the licence's own page where it has
// one, and its author as its file page credits them (null where its licence asks for none), which
// the page lists for attribution. Absent: the page is the card list alone.
export interface ArticlesReaderInfo {
  sha256: string;
  bytes: number;
  marks: number;
  passages: number;
  unplaced: string[];
  images?: Array<{ file: string; project: string; licence: string | null; licence_url?: string | null; author?: string | null }>;
}

export interface ArticlesPassage {
  id: string;
  item: string;
  // A /2 store's revision-free passage key (build_store); absent from /1 stores.
  passage_key?: string;
  kind: string;
  // The passage's heading path ([] in the lead) and prominence
  // (subject | central | supporting): every record of the agreed format carries
  // both. Null only in a store built before they were required.
  section: string[] | null;
  prominence: string | null;
  quote: { exact: string; prefix?: string; suffix?: string };
  // True when the quote is the capped head of a longer passage: the export
  // quotes at most a passage's first 12 words (build_store.quote_capped).
  // Absent from stores built before it; the page then reads a quote at the
  // cap as a head (articles-render quoteCapped).
  quote_capped?: boolean;
  // True when the quote is such a head and the rest of its passage lies
  // within the quote's suffix (build_store.quote_extent), so the card, which
  // shows the suffix, shows where the passage ends. Absent from stores built
  // before it; the card then reads a head's passage as going on past it.
  passage_ends_in_suffix?: boolean;
  subject_uri: string;
  outcome: "located" | "absent" | "unresolved";
  status: string | null;
  // location_key of the location that justifies the status (build_store
  // derive_status), or null (also when the coverage claim justifies it);
  // absent from stores built before it existed.
  basis?: string | null;
  // Decision D1's coverage claim: hasPart locations (`parts`, location keys)
  // that together state every clause. It counts only once its challenge is
  // "survived". `status` is what the claim gives on its own (formalized or
  // statement_only once it survived, null otherwise) and `basis` says it, not one
  // location, justifies the passage's status. Null when the passage has none;
  // absent from stores built before D1.
  coverage?: ArticlesCoverage | null;
  locations: ArticlesLocation[];
  absence: {
    universe: string;
    universe_sha256: string;
    libraries: number;
    receipt: { sha256: string; queries: number; candidates: number };
    challenge: string;
  } | null;
  unresolved: { challenge: string } | null;
  // Optional (build_store --passage-text): the passage in full, the layer slice
  // its record's span_sha256 hashes (`layer`) and the same text as segments the
  // page renders, formulas from their TeX. Absent: the page shows the quote.
  text?: {
    layer: string; segments: PassageSegment[];
    // The rest of the sentence around a passage that is only part of one (and, for a
    // list item or a lower-case fragment, the sentence that leads into it), from the
    // same local copy of the revision (build_store passage_text_entry).
    before?: PassageContext; after?: PassageContext;
  } | null;
}

export interface PassageContext { layer: string; segments: PassageSegment[]; trimmed?: boolean; lead_in?: boolean }

export interface ArticlesCoverage {
  complete: boolean;
  parts: string[];
  challenge: string;
  status: string | null;
  basis: boolean;
  // A /2 store's bundle: its relation (its weakest part's), its conjuncts beside
  // the part (location key) stating each, and its key. Absent from /1 stores.
  relation?: string;
  conjuncts?: Array<{ id: string; text: string; part: string }>;
  bundle_key?: string;
}

// One prominence's passages and their statuses (build_store._prominence_counts).
export interface ProminenceCounts {
  passages: number;
  by_status: Record<SummaryStatus, number>;
}

export interface LibraryCounts {
  locations: number;
  // locations split by proof status: complete = proved or a definition,
  // incomplete = statement_only, wanted or axiom_dependent.
  complete?: number;
  incomplete?: number;
  passages: number;
  related: number;
}

export interface ArticlesArticle {
  schema: string;
  pageid: number;
  title: string;
  revision: number;
  source_id: string;
  wikidata: string | null;
  assessment: { class?: string; importance?: string } | null;
  campaign: string;
  universe: { id: string; sha256: string; frozen: string; libraries: number };
  record_set: string;
  records_sha256: string;
  // The licences of what the view holds (build_store MAPPING_LICENCE): the text it quotes is
  // under `text`, its records' subject licence (an SPDX id, null for an article with no
  // records), and the mapping under `mapping`, CC0-1.0. Absent from stores built before it.
  licence?: { text: string | null; mapping: string } | null;
  // A /2 store's seal summary (build_store.seal_summary), which the page's
  // Details cite; absent from /1 stores. The seal records no model or operator.
  seal?: { set_id: string; schema: string; status_rule: number; created: string; skill_commit: string } | null;
  // Optional (build_store --provenance): the runs that made this article's
  // records, which the Details folds cite by model, operator and date, and the
  // inventory run that chose its passages, when the locate run names one.
  provenance?: { locate: ArticlesRun; challenge: ArticlesRun | null; inventory?: ArticlesRun | null } | null;
  supplements?: ArticlesSupplements | null;
  // Optional (build_store --reader-html): the article itself, marked (ArticlesReaderInfo).
  reader?: ArticlesReaderInfo | null;
  // Optional (build_store --audit-summary): what an audit of the build measured (ArticlesAudit).
  audit?: ArticlesAudit | null;
  summary: {
    passages: number;
    // Locations exclude relatedMatch declarations, which are counted in `related`.
    locations: number;
    related: number;
    by_status: Record<SummaryStatus, number>;
    // subject | central | supporting; absent from stores built before it.
    by_prominence?: Record<string, ProminenceCounts>;
    by_library: Record<string, LibraryCounts>;
    by_proof_status: Record<string, number>;
    by_relation: Record<string, number>;
  };
  passages: ArticlesPassage[];
}

export interface ArticlesUniverse {
  schema: string;
  id: string;
  sha256: string;
  frozen: string;
  // Sorted by id, as the record set's universe.json lists them.
  libraries: UniverseLibrary[];
  excluded: Array<{ id: string; reason: string }>;
}

export interface ArticlesCurrent {
  schema: string;
  campaign: string;
  built_at: string;
  universe: { id: string; sha256: string; frozen: string; libraries: number };
  // set_id and byte_budget (0 = the export had none) come from SEAL.json; a /2
  // store adds the seal's schema and status_rule.
  record_set: ArticlesRecordSet;
  // A build of several record sets lists each (build_store --record-set ... --campaign).
  record_sets?: Array<ArticlesRecordSet & { campaign: string }>;
  // Optional (build_store --provenance and the other side files): every run the
  // build's articles cite, and what was merged, with counts.
  provenance?: { runs: ArticlesRun[] } | null;
  supplements?: ArticlesSupplements | null;
  audit?: ArticlesAudit | null;
  counts: {
    articles: number;
    passages: number;
    locations: number;
    related: number;
    by_status: Record<SummaryStatus, number>;
    // Every article's summary.by_prominence summed; absent from stores built before it.
    by_prominence?: Record<string, ProminenceCounts>;
    by_library: Record<string, LibraryCounts & { articles: number }>;
    by_importance: Record<string, { articles: number; passages: number; by_status: Record<SummaryStatus, number> }>;
  };
  index: { hex: number; shards: string[]; entries: number };
}

export interface ArticlesRecordSet {
  seal_sha256: string;
  set_id?: string;
  created: string;
  skill_commit: string;
  byte_budget?: number;
  schema?: string;
  status_rule?: number;
}

export interface IndexEntry {
  title: string;
  pageid: number;
  revision: number;
  passages: number;
  formalized: number;
  statement_only: number;
  partial: number;
  not_formalized: number;
  unresolved: number;
  importance: string | null;
  // Ids of the libraries holding a location for the article, in universe order.
  libraries: string[];
}

// ---- the store reader ------------------------------------------------------

// One method, so R2, a static asset tree or an in-memory map can back it.
export interface ArticlesReader {
  // `key` is relative to articles/v1/; null = no such object.
  text(key: string): Promise<string | null>;
}

export function r2Reader(bucket: Pick<R2Bucket, "get">): ArticlesReader {
  return {
    async text(key: string) {
      const object = await bucket.get(ARTICLES_PREFIX + key);
      return object ? object.text() : null;
    },
  };
}

// Alternatives to R2, which backs the store (the ARTICLES binding in
// wrangler.jsonc; docs/ARTICLES.md section 14, item 2). Neither is wired to a
// binding; adopting one is a one-line change in articlesReader plus its binding.
// KV: one value per store path, the same keys as the R2 objects.
export function kvReader(namespace: Pick<KVNamespace, "get">): ArticlesReader {
  return { text: (key: string) => namespace.get(ARTICLES_PREFIX + key) };
}

// Static assets: the tree copied under wiki/public/articles-store/ and served by
// the ASSETS binding (release-coupled: it would ship only with a release).
export function staticReader(assets: Pick<Fetcher, "fetch">, origin = "https://assets.invalid"): ArticlesReader {
  return {
    async text(key: string) {
      const res = await assets.fetch(new Request(`${origin}/articles-store/${ARTICLES_PREFIX}${key}`));
      return res.ok ? res.text() : null;
    },
  };
}

// Test and preview backend: keys are full store paths ("articles/v1/...").
export function memoryReader(files: ReadonlyMap<string, string> | Record<string, string>): ArticlesReader {
  const map = files instanceof Map ? files : new Map(Object.entries(files));
  return { text: async (key: string) => map.get(ARTICLES_PREFIX + key) ?? null };
}

// One reader per bound bucket object, so the store-object cache below survives
// across the requests an isolate serves with that binding. (Were the runtime to
// hand each request a fresh binding object, the cache would simply miss.)
const READERS = new WeakMap<object, ArticlesReader>();

export function articlesReader(env: Env): ArticlesReader | null {
  if (!env.ARTICLES) return null;
  let reader = READERS.get(env.ARTICLES);
  if (!reader) {
    reader = r2Reader(env.ARTICLES);
    READERS.set(env.ARTICLES, reader);
  }
  return reader;
}

export class ArticlesStoreError extends Error {}

function parseJson<T extends { schema: string }>(text: string, key: string, schema: string): T {
  let value: T;
  try {
    value = JSON.parse(text) as T;
  } catch {
    throw new ArticlesStoreError(`articles store object ${key} is not JSON`);
  }
  if (!value || typeof value !== "object" || value.schema !== schema) {
    throw new ArticlesStoreError(`articles store object ${key} is not ${schema}`);
  }
  return value;
}

async function readJson<T extends { schema: string }>(reader: ArticlesReader, key: string, schema: string): Promise<T | null> {
  const text = await reader.text(key);
  return text === null ? null : parseJson<T>(text, key, schema);
}

// The store's small objects (current.json, a universe, an index shard), per reader, for
// INDEX_CACHE_MS: each read is kept with its text, which the JSON routes serve as stored, and its
// parsed value, null for a missing object. Without it every anonymous view would re-read them from
// R2 (review finding: about 18 Class-B reads and several MB of parsing per dashboard view, and one
// read per request on the JSON routes). Only settled values are kept, never a pending promise,
// since a Worker must not await another request's I/O, and never a failed read (R2 answered with
// an error, which the next request tries again). An object that was read but does not parse is
// kept as that failure, which every request in the minute re-throws: the dashboard and the
// universe page take no read budget, so a damaged object must not cost one R2 read per request.
// A newly published store, a withdrawn one (current.json deleted) or a repaired object shows
// within a minute, the staleness /api/articles/current already allows (max-age=60).
export const INDEX_CACHE_MS = 60_000;
interface CachedObject<T> { at: number; text: string | null; value: T | null; failure?: unknown }
const OBJECT_CACHE = new WeakMap<ArticlesReader, Map<string, CachedObject<unknown>>>();

async function cachedObject<T>(reader: ArticlesReader, key: string, parse: (text: string) => T, now: number): Promise<CachedObject<T>> {
  // Created and registered before any await: concurrent loads of one request must share one
  // map, or the last to finish would drop the others.
  let cache = OBJECT_CACHE.get(reader);
  if (!cache) {
    cache = new Map();
    OBJECT_CACHE.set(reader, cache);
  }
  const hit = cache.get(key) as CachedObject<T> | undefined;
  if (hit && now - hit.at >= 0 && now - hit.at < INDEX_CACHE_MS) {
    if ("failure" in hit) throw hit.failure;
    return hit;
  }
  const text = await reader.text(key);
  let entry: CachedObject<T>;
  try {
    entry = { at: now, text, value: text === null ? null : parse(text) };
  } catch (failure) {
    cache.set(key, { at: now, text: null, value: null, failure });
    throw failure;
  }
  cache.set(key, entry);
  return entry;
}

const PAGEID_RE = /^[1-9][0-9]{0,9}$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const SHARD_RE = /^[0-9a-f]{1,2}$/;
const LIBRARY_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export function parsePageid(raw: string | undefined): number | null {
  if (!raw || !PAGEID_RE.test(raw)) return null;
  const value = Number(raw);
  return value <= 2 ** 31 ? value : null;
}

// current.json is the store's publish switch: every articles route but the feedback export, and the
// article box, answer as if the store held nothing until it is there, and again within a minute
// of its deletion (docs/ARTICLES.md, "Publishing"), whatever other objects the bucket still holds.
const loadCurrentObject = (reader: ArticlesReader, now = Date.now()) =>
  cachedObject(reader, "current.json", (text) => parseJson<ArticlesCurrent>(text, "current.json", CURRENT_SCHEMA), now);
export const loadCurrent = async (reader: ArticlesReader, now = Date.now()) => (await loadCurrentObject(reader, now)).value;
export const loadArticle = (reader: ArticlesReader, pageid: number) =>
  readJson<ArticlesArticle>(reader, `articles/${pageid}.json`, ARTICLE_SCHEMA);
const loadUniverseObject = (reader: ArticlesReader, sha: string, now = Date.now()) =>
  cachedObject(reader, `universes/${sha}.json`,
    (text) => parseJson<ArticlesUniverse>(text, `universes/${sha}.json`, UNIVERSE_SCHEMA), now);
export const loadUniverse = async (reader: ArticlesReader, sha: string, now = Date.now()) =>
  SHA_RE.test(sha) ? (await loadUniverseObject(reader, sha, now)).value : null;

// Whether the article's view names a marked revision (build_store --reader-html): a sha256
// of 64 hex and a valid page id. The card list needs no more to offer the reader.
export function readerNamed(article: ArticlesArticle): boolean {
  const info = article.reader;
  return !!info && typeof info === "object" && typeof info.sha256 === "string" && SHA_RE.test(info.sha256)
    && parsePageid(String(article.pageid)) !== null;
}

// The article's marked revision, or null: when the view names none, when the file is
// missing, or when its bytes are not the ones the view names (a store written in part). The
// path is built from the page id, never read from the store.
export async function loadReaderHtml(reader: ArticlesReader, article: ArticlesArticle): Promise<string | null> {
  if (!readerNamed(article)) return null;
  const info = article.reader as ArticlesReaderInfo;
  const pageid = parsePageid(String(article.pageid)) as number;
  const html = await reader.text(`reader/${pageid}.html`);
  if (html === null) return null;
  if ((await sha256Hex(html)) !== info.sha256) {
    console.log(JSON.stringify({ event: "articles-reader-mismatch", pageid }));
    return null;
  }
  return html;
}

// Parsed index shards, in the store-object cache (INDEX_CACHE_MS): the dashboard reads every
// shard and each article box one, and /api/articles/index/:shard serves a shard's text from there.
const loadIndexShardObject = (reader: ArticlesReader, shard: string, now = Date.now()) =>
  cachedObject(reader, `index/${shard}.json`, (text) => parseIndexShard(text, shard), now);

export async function loadIndexShard(reader: ArticlesReader, shard: string, now = Date.now()): Promise<IndexEntry[] | null> {
  if (!SHARD_RE.test(shard)) return null;
  return (await loadIndexShardObject(reader, shard, now)).value;
}

function parseIndexShard(text: string, shard: string): IndexEntry[] {
  const raw = parseJson<{ schema: string; fields: string[]; articles: unknown[][] }>(text, `index/${shard}.json`, INDEX_SCHEMA);
  if (!Array.isArray(raw.fields) || !Array.isArray(raw.articles)) {
    throw new ArticlesStoreError(`articles index shard ${shard} lacks fields/articles`);
  }
  const at = (name: string) => raw.fields.indexOf(name);
  const pos = {
    title: at("title"), pageid: at("pageid"), revision: at("revision"), passages: at("passages"),
    formalized: at("formalized"), statement_only: at("statement_only"), partial: at("partial"),
    not_formalized: at("not_formalized"), unresolved: at("unresolved"), importance: at("importance"),
    libraries: at("libraries"),
  };
  if (pos.title < 0 || pos.pageid < 0) throw new ArticlesStoreError(`articles index shard ${shard} lacks title/pageid`);
  const num = (row: unknown[], i: number) => (i >= 0 && Number.isSafeInteger(row[i]) && (row[i] as number) >= 0 ? (row[i] as number) : 0);
  const libraries = (row: unknown[]): string[] => {
    const value = pos.libraries >= 0 ? row[pos.libraries] : null;
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string" && LIBRARY_ID_RE.test(id)).slice(0, 64)
      : [];
  };
  return raw.articles.filter(Array.isArray).map((row) => ({
    title: String(row[pos.title]),
    pageid: num(row, pos.pageid),
    revision: num(row, pos.revision),
    passages: num(row, pos.passages),
    formalized: num(row, pos.formalized),
    statement_only: num(row, pos.statement_only),
    partial: num(row, pos.partial),
    not_formalized: num(row, pos.not_formalized),
    unresolved: num(row, pos.unresolved),
    importance: pos.importance >= 0 && typeof row[pos.importance] === "string" ? (row[pos.importance] as string) : null,
    libraries: libraries(row),
  }));
}

// Title comparison form, byte-identical to build_store.title_key: NFC,
// underscores and the listed spaces collapsed to one space, trimmed.
const TITLE_SPACE = /[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_]+/g;
export function titleKey(title: string): string {
  return title.normalize("NFC").replace(TITLE_SPACE, " ").replace(/^ +| +$/g, "");
}

async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// v1 stores shard on the first hex digit (build_store.INDEX_HEX).
export const INDEX_HEX = 1;
export async function titleShard(title: string, hex = INDEX_HEX): Promise<string> {
  return (await sha256Hex(titleKey(title))).slice(0, hex);
}

// One index-shard read: the articles entry for a Wikipedia title, or null.
export async function lookupTitle(reader: ArticlesReader, title: string): Promise<IndexEntry | null> {
  const key = titleKey(title);
  if (!key) return null;
  const entries = await loadIndexShard(reader, await titleShard(key));
  return entries?.find((entry) => titleKey(entry.title) === key) ?? null;
}

// URL slug for /articles/:pageid/:slug (readability only; the page id is the key).
export function articlesSlug(title: string): string {
  return encodeURIComponent(titleKey(title).replaceAll(" ", "_"));
}

export function articlesHref(pageid: number, title: string): string {
  return `/articles/${pageid}/${articlesSlug(title)}`;
}

// The article page's compact summary box (pages.ts injectArticlesSummary). Never
// throws: an unbound, unpublished (no current.json), empty or broken store
// simply means no box.
export async function articlesSummaryFor(env: Env, wikipediaTitle: string): Promise<IndexEntry | null> {
  const reader = articlesReader(env);
  if (!reader) return null;
  try {
    if (!(await loadCurrent(reader))) return null;
    return await lookupTitle(reader, wikipediaTitle);
  } catch (error) {
    console.log(JSON.stringify({ event: "articles-box-error", msg: String(error) }));
    return null;
  }
}

// ---- responses -------------------------------------------------------------

export const NOT_CONFIGURED = "articles store not configured";
export const NOT_PUBLISHED = "articles store has no published build";
export const STORE_UNREADABLE = "an articles store object could not be read";
const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=300" };

// Every articles page is the articles shell (articles-render.ts), and every one enforces the
// plane's own policy, security.ts articlesCsp, for the page's own origin; the security
// middleware lists the framing ban beside it. Every HTML answer below goes through here.
function articlesHtml(
  c: Ctx,
  html: string,
  status: ContentfulStatusCode = 200,
  headers: Record<string, string> = {},
  turnstile = false,
): Response {
  return c.html(html, status, { ...headers, "Content-Security-Policy": articlesCsp(new URL(c.req.url).origin, turnstile) });
}

function notConfiguredJson(c: Ctx, error = NOT_CONFIGURED): Response {
  return c.json({ ok: false, error }, 503, { "Cache-Control": "no-store" });
}

const UNAVAILABLE_DETAIL: Record<string, string> = {
  [NOT_CONFIGURED]: "This deployment has no articles store bound (docs/ARTICLES.md, “Store”).",
  [NOT_PUBLISHED]: "The articles store holds no published build (docs/ARTICLES.md, “Publishing”).",
  [STORE_UNREADABLE]: "Try again shortly.",
};

function notConfiguredHtml(c: Ctx, error = NOT_CONFIGURED): Response {
  return articlesHtml(c, articlesMessagePage("Articles unavailable", `The articles plane is unavailable: ${error}.`,
    UNAVAILABLE_DETAIL[error] ?? ""), 503, { "Cache-Control": "no-store" });
}

// A store object that does not parse, or does not have the shape the pages read (a truncated
// upload, a damaged object): the plane's own 503 page or JSON, never the site's generic 500, and
// the error goes only to the log. That includes a string no URL can carry (a lone surrogate, which
// JSON can hold and encodeURIComponent refuses with a URIError) and a value out of every range the
// pages read (a RangeError). Anything else is a bug and propagates.
function isStoreError(error: unknown): boolean {
  return error instanceof ArticlesStoreError || error instanceof SyntaxError || error instanceof TypeError
    || error instanceof URIError || error instanceof RangeError;
}

async function storeGuard(c: Ctx, json: boolean, where: Record<string, unknown>, run: () => Promise<Response>): Promise<Response> {
  try {
    return await run();
  } catch (error) {
    if (!isStoreError(error)) throw error;
    console.log(JSON.stringify({ event: "articles-store-error", ...where, msg: String(error) }));
    return json ? notConfiguredJson(c, STORE_UNREADABLE) : notConfiguredHtml(c, STORE_UNREADABLE);
  }
}

// One store read of a route that also writes D1 (feedback): a store error is the plane's 503
// JSON, and anything the route does besides is left to the site's error handler.
async function storeRead<T>(c: Ctx, read: () => Promise<T>): Promise<T | Response> {
  try {
    return await read();
  } catch (error) {
    if (!isStoreError(error)) throw error;
    console.log(JSON.stringify({ event: "articles-store-error", route: "feedback", msg: String(error) }));
    return notConfiguredJson(c, STORE_UNREADABLE);
  }
}

// A store list: anything but an array is empty (a store object is cast, not validated).
function arrayOf<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

// ---- feedback --------------------------------------------------------------

const FEEDBACK_VERDICTS = new Set(["confirm", "report", "withdraw"]);
// The domain that keeps these pseudonyms apart from the research export's. Its value is
// the D1 table's name, which the 2026-11-05 rename left alone, so no pseudonym is re-keyed.
export const ARTICLES_PSEUDONYM_DOMAIN = "atlas-feedback\0";
const MAX_NOTE_CHARS = MAX_FLAG_COMMENT_LEN; // 500, the flag form's cap
// SEAL.json's campaign, as build_store.CAMPAIGN_RE: a campaign id, or for a run
// outside any campaign "run:<run-id>[+<challenge-run-id>]".
export const CAMPAIGN_RE = /^(?:run:[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\+[A-Za-z0-9][A-Za-z0-9._-]{0,63})?|(?!run:)[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$/;

function missingTable(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const message = current instanceof Error ? current.message : String(current);
    if (message.includes("no such table: atlas_feedback")) return true;
    current = typeof current === "object" && current !== null && "cause" in current ? (current as { cause?: unknown }).cause : null;
  }
  return false;
}

// The viewer's current verdicts on one article in one campaign (location_key →
// verdict): a verdict names the campaign the person saw, so it is not shown in
// the next, and a withdrawn one is no verdict. Empty when the table is missing
// (migration 0015 not yet applied).
async function viewerVerdicts(env: Env, userId: string, pageid: number, campaign: string): Promise<Map<string, string>> {
  try {
    const { results } = await env.DB.prepare(
      "SELECT location_key, verdict FROM atlas_feedback_latest WHERE user_id = ? AND pageid = ? AND campaign = ? AND verdict <> 'withdraw'",
    ).bind(userId, pageid, campaign).all<{ location_key: string; verdict: string }>();
    return new Map(results.map((row) => [row.location_key, row.verdict]));
  } catch (error) {
    if (!missingTable(error)) throw error;
    return new Map();
  }
}

// The WikiLean article of an articles article's Wikipedia title, for the page's
// "Annotated article on WikiLean" link. Migration 0016 indexes
// articles.wikipedia_title, so a lookup reads the matching rows (without it, the
// whole table: the query is the same). Each isolate keeps what it read, a title with
// no WikiLean article included, for SLUG_CACHE_MS, and at most MAX_SLUG_CACHE
// titles (the one read longest ago goes first), so a page read again within a
// minute costs no D1 read and a new WikiLean article gets its link within a
// minute. The cache is per D1 binding object, as the store-object cache is per
// reader; it keeps only settled values, never a pending query, and never an
// error.
export const SLUG_CACHE_MS = INDEX_CACHE_MS;
export const MAX_SLUG_CACHE = 1024;
const SLUG_CACHE = new WeakMap<object, Map<string, { at: number; slug: string | null }>>();

export async function wikileanSlugFor(env: Pick<Env, "DB">, title: string, now = Date.now()): Promise<string | null> {
  const key = titleKey(title);
  let cache = SLUG_CACHE.get(env.DB);
  if (!cache) {
    cache = new Map();
    SLUG_CACHE.set(env.DB, cache);
  }
  // The map's order is the order of last use: a title read goes last, so a full
  // cache drops the title read longest ago, never a page that is being read.
  const hit = cache.get(key);
  cache.delete(key);
  if (hit && now - hit.at >= 0 && now - hit.at < SLUG_CACHE_MS) {
    cache.set(key, hit);
    return hit.slug;
  }
  const { results } = await env.DB.prepare(
    "SELECT slug FROM articles WHERE wikipedia_title IN (?, ?) ORDER BY slug LIMIT 1",
  ).bind(key, key.replaceAll(" ", "_")).all<{ slug: string }>();
  const slug = results[0]?.slug ?? null;
  cache.set(key, { at: now, slug });
  for (const oldest of cache.keys()) {
    if (cache.size <= MAX_SLUG_CACHE) break;
    cache.delete(oldest);
  }
  return slug;
}

function foldForSearch(s: string): string {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

// Sort and search keys, computed once per cached index entry.
const ENTRY_KEYS = new WeakMap<IndexEntry, { key: string; folded: string }>();
function entryKeys(entry: IndexEntry): { key: string; folded: string } {
  let keys = ENTRY_KEYS.get(entry);
  if (!keys) {
    keys = { key: titleKey(entry.title), folded: foldForSearch(entry.title) };
    ENTRY_KEYS.set(entry, keys);
  }
  return keys;
}

const MAX_DASHBOARD_ROWS = 200;

// The anonymous read budget /api/brain/* and /mcp use: per IP (an IPv6 client by
// its /64, limiterIp), MCP_LIMITER when bound, else BRAIN_API_LIMITER (120/min),
// under a key prefix of its own per use. A ?q= search scans every index entry
// (articles-q:<ip>). An article view reads its view model, its universe and its
// marked revision from the store, hashes the revision and renders it, and its
// two JSON views serve the view model and the records (articles-read:<ip>, one
// budget for the three). The small JSON objects (current.json, the published
// universe, an index shard) share a budget of their own (articles-meta:<ip>), so the
// dashboard's live filter, which fetches every shard, never spends an article's.
// The dashboard without a search and the universe page take none: they read only
// those small objects, through the store-object cache, at most once a minute each,
// whether an object parses or not (a damaged one is kept as its failure).
type ReadBudget = "articles-q" | "articles-read" | "articles-meta";
async function readAllowed(c: Ctx, budget: ReadBudget): Promise<boolean> {
  const limiter = c.env.MCP_LIMITER ?? c.env.BRAIN_API_LIMITER;
  const ip = limiterIp(c.req.header("CF-Connecting-IP")) || "unknown";
  const { success } = await limiter.limit({ key: `${budget}:${ip}` });
  return success;
}

// Over the budget: 429 with Retry-After (the limiters' period is 60 seconds),
// never stored by a cache, and saying nothing of how the budget is kept.
const RETRY_AFTER_SECONDS = 60;
const LIMITED_HEADERS = { "Retry-After": String(RETRY_AFTER_SECONDS), "Cache-Control": "no-store" };

function limitedJson(c: Ctx): Response {
  return c.json({ ok: false, error: "too many requests from this address; try again in a minute" }, 429, LIMITED_HEADERS);
}

function limitedHtml(c: Ctx, title: string, message: string, detail: string): Response {
  return articlesHtml(c, articlesMessagePage(title, message, detail), 429, LIMITED_HEADERS);
}

// The index shards current.json names (the builder lists every shard it wrote), at most
// MAX_INDEX_SHARDS: what the dashboard reads and /api/articles/index serves.
function publishedShards(current: ArticlesCurrent): string[] {
  return (Array.isArray(current.index?.shards) ? current.index.shards : [])
    .filter((s) => typeof s === "string" && SHARD_RE.test(s)).slice(0, MAX_INDEX_SHARDS);
}

// ---- community tags: the overlay write surface -----------------------------
//
// Three routes (the signed public-editing plan, section 2.2): add or change a
// tag, change its state (the soft delete and its undo), and read one article's
// overlay. Live on save, account required, same-origin, byte-capped, rate
// limited, optimistically concurrent, and never a write into the sealed store.
//
// The order of every check is the house one: same-origin, identity, the
// site-wide brake, the budget, the body, then the store. A refused write costs
// at most one D1 read and no R2 read.

export const TAG_PATH = "/api/articles/tag";
export const CC0_REQUIRED = "the CC0 agreement is required before your first edit";
export const TURNSTILE_REQUIRED = "a Turnstile check is required for a new account's first edit";
export const PIPELINE_REFUSED = "the pipeline bearer does not write the community overlay";
export const AGENT_HUMAN_ROW_REFUSED =
  "agents may not change or remove an editor's tag: release R1 keeps no history to hold the editor's version";
export const NO_PASSAGE_KEYS =
  "this article's record set carries no passage keys (a /1 set); community tags need a /2 set";

// What an overlay write has established about its caller before it reads a body.
interface TagWriter {
  user: AuthUser;
  // Whose tier, daily cap and CC0 agreement govern the write: an agent's owner
  // (PR #126's agent_tokens.owner_id), else the person themself. An agent
  // inherits its owner's standing and never exceeds it.
  account: string;
  actorType: TagActor;
  via: "web" | "api";
  allowance: TagAllowance;
}

const RETRY_AFTER_DAY = { "Retry-After": "3600", "Cache-Control": "no-store" };

async function beginTagWrite(c: Ctx): Promise<TagWriter | Response> {
  const originErr = checkOrigin(c);
  if (originErr) return originErr;
  const user = await getUser(c);
  if (!user) return c.json({ ok: false, error: "login required" }, 401);
  // The pipeline bearer writes the sealed store, not the overlay: a community
  // row under its name would be a pipeline location wearing a community badge.
  // Agents may write here (decision 13); their atlas_feedback refusal stands.
  if (user.role === "bot") return c.json({ ok: false, error: PIPELINE_REFUSED }, 403);
  const braked = await writeBrake(c);
  if (braked) return braked;

  const isAgent = user.role === "agent";
  const account = accountFor(user);
  const allowance = await tagAllowance(c.env, account, Date.now());
  // Burst. All of one person's agents share PR #126's single 30/min key across
  // saves, Brain writes and now overlay writes, so five agents are not five
  // budgets. A person gets TAG_LIMITER, under a newcomer or an established key.
  const limiter = isAgent
    ? c.env.EDIT_LIMITER
    : (allowance.tier === "new" ? c.env.TAG_NEW_LIMITER : null) ?? c.env.TAG_LIMITER ?? c.env.EDIT_LIMITER;
  const key = isAgent ? editLimitKey(user) : tagLimitKey(user, allowance.tier);
  const { success } = await limiter.limit({ key });
  if (!success) {
    return c.json({ ok: false, error: "rate limited — slow down", tier: allowance.tier }, 429,
      { "Retry-After": String(RETRY_AFTER_SECONDS), "Cache-Control": "no-store" });
  }
  // Daily volume (plan section 2.6). A spree from a fresh account is bounded at
  // 50 rows before anyone has to be awake.
  if (allowance.today >= allowance.cap) {
    return c.json({
      ok: false,
      error: `daily limit reached: a ${allowance.tier} account may make ${allowance.cap} overlay edits a day`,
      tier: allowance.tier, cap: allowance.cap,
    }, 429, RETRY_AFTER_DAY);
  }
  return { user, account, actorType: isAgent ? "agent" : "human", via: isAgent ? "api" : "web", allowance };
}

// The CC0 agreement at the point of a person's FIRST overlay edit (decision 11),
// and the Turnstile check that rides with it (plan section 2.6: a new account's
// first edit only, never after). Returns the timestamp to record, or a Response.
//
// An agent cannot agree to a licence; its OWNER can, and must have, before any
// of that person's agents may write (plan section 5.3, "Licence").
type AgreementStep = { needsRecord: false } | { needsRecord: true; verifiedAt: number | null };

async function requireAgreement(
  c: Ctx,
  writer: TagWriter,
  body: Record<string, unknown>,
  now: number,
): Promise<AgreementStep | Response> {
  const agreed = await loadAgreement(c.env, writer.account);
  if (agreed) return { needsRecord: false };
  if (writer.actorType === "agent") {
    return c.json({
      ok: false,
      error: "the person who minted this agent has not accepted the CC0 terms; they must make one edit themself first",
    }, 403);
  }
  if (body.cc0 !== true) {
    return c.json({
      ok: false, error: CC0_REQUIRED, cc0_required: true,
      cc0_version: CC0_AGREEMENT_VERSION, cc0_text: CC0_AGREEMENT_TEXT,
    }, 403);
  }
  const token = typeof body.turnstile_token === "string" && body.turnstile_token !== "" ? body.turnstile_token : null;
  const verdict = await verifyTurnstile(c.env, token);
  if (verdict === "failed") {
    return c.json({ ok: false, error: TURNSTILE_REQUIRED, turnstile_required: true }, 403);
  }
  return { needsRecord: true, verifiedAt: verdict === "ok" ? now : null };
}

// An optional free-text field: null, or a Response when it is too long or not a
// string. Trimmed, and an empty string reads as absent.
function optionalText(c: Ctx, value: unknown, max: number, field: string): string | null | Response {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string" || value.length > max) {
    return c.json({ ok: false, error: `${field} must be a string of at most ${max} characters` }, 400);
  }
  return value.trim() || null;
}

// The pipeline location at a pair key, anywhere in the article, or null.
function locationAt(article: ArticlesArticle, key: string): { passage: ArticlesPassage; loc: ArticlesLocation } | null {
  for (const passage of arrayOf(article.passages)) {
    for (const loc of arrayOf(passage?.locations)) {
      if (loc && typeof loc === "object" && loc.pair_key === key) return { passage, loc };
    }
  }
  return null;
}

// Whether the article's view model carries the /2 keys an overlay needs.
function hasPassageKeys(article: ArticlesArticle): boolean {
  return arrayOf(article.passages).some((p) => typeof p?.passage_key === "string");
}

// The passage's status after the write, recomputed over the merged set, so the
// page can move the headline without a reload.
async function statusAfter(env: Env, article: ArticlesArticle, passage: ArticlesPassage): Promise<SummaryStatus> {
  const tags = await loadOverlayTags(env, article.pageid);
  const merged = mergeOverlay(article, { tags, authors: new Map() }, statusRuleOf(article));
  return merged.byPassage.get(passage.id)?.status ?? "unresolved";
}

function tagMissingTable(c: Ctx, error: unknown): Response | null {
  if (!overlayTableMissing(error)) return null;
  return notConfiguredJson(c, "community tags are not configured (migration 0019)");
}

// ---- routes ----------------------------------------------------------------

// Search engines are asked to index nothing of the articles plane: a reader page copies a
// whole pinned Wikipedia revision, a card list its passages, and the JSON views
// the same text. Every answer under /articles and /api/articles/ carries X-Robots-Tag,
// whatever its status (the site's 404 for a path the plane does not route among
// them), and every articles page also says so in a robots meta tag (articles-render.ts
// shell). Links on the pages are still followed. /api/articles itself is NOT this
// plane's — it is the header search box's index of WikiLean's own articles
// (index.ts) — so it is left exactly as it was, whatever a trailing wildcard
// matches.
export const ARTICLES_ROBOTS = "noindex";
export const ARTICLES_ROBOTS_PATHS: readonly string[] = ["/articles", "/articles/*", "/api/articles/*"];
export const ARTICLE_INDEX_PATH = "/api/articles";

export function registerArticlesRoutes(app: Hono<{ Bindings: Env }>): void {
  // First, so it wraps every articles route below.
  for (const path of ARTICLES_ROBOTS_PATHS) {
    app.use(path, async (c, next) => {
      await next();
      if (new URL(c.req.url).pathname === ARTICLE_INDEX_PATH) return;
      try {
        c.res.headers.set("X-Robots-Tag", ARTICLES_ROBOTS);
      } catch {
        // Immutable headers (a Response passed through from a fetch): re-wrap.
        c.res = new Response(c.res.body, c.res);
        c.res.headers.set("X-Robots-Tag", ARTICLES_ROBOTS);
      }
    });
  }

  // Store objects keep their builder's bytes; the API serves them as stored, once they are what the
  // store says they are (an article view that parses as one, records whose sha256 is the one their
  // view names), so a damaged or partly written object is the plane's 503 with no-store
  // (storeGuard), as the pages answer it, never a 200 that browsers and caches keep.
  const serveStored = (c: Ctx, text: string, contentType: string) =>
    c.body(text, 200, { "Content-Type": contentType, "Cache-Control": JSON_HEADERS["Cache-Control"] });
  // A small object's text from the store-object cache.
  const serveCached = (c: Ctx, text: string | null, cache: string, missing: string) =>
    text === null ? c.json({ ok: false, error: missing }, 404)
      : c.body(text, 200, { "Content-Type": JSON_HEADERS["Content-Type"], "Cache-Control": cache });

  // current.json, the published universe and the published index shards, under the meta budget.
  // Only what current.json names is served: another sha or shard is a 404 that reads nothing.
  app.get("/api/articles/current", (c) => storeGuard(c, true, { route: "current" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    if (!(await readAllowed(c, "articles-meta"))) return limitedJson(c);
    const current = await loadCurrentObject(reader);
    if (current.value === null) return notConfiguredJson(c, NOT_PUBLISHED);
    return serveCached(c, current.text, "public, max-age=60", NOT_PUBLISHED);
  }));

  // The view model and the records take the article's read budget before any
  // store read. The records route also reads the view, which names their sha256.
  app.get("/api/articles/article/:pageid", (c) => storeGuard(c, true, { route: "article" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    if (!(await readAllowed(c, "articles-read"))) return limitedJson(c);
    if (!(await loadCurrent(reader))) return notConfiguredJson(c, NOT_PUBLISHED);
    const pageid = parsePageid(c.req.param("pageid"));
    if (pageid === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    const key = `articles/${pageid}.json`;
    const text = await reader.text(key);
    if (text === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    parseJson<ArticlesArticle>(text, key, ARTICLE_SCHEMA);
    return serveStored(c, text, JSON_HEADERS["Content-Type"]);
  }));

  app.get("/api/articles/records/:pageid", (c) => storeGuard(c, true, { route: "records" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    if (!(await readAllowed(c, "articles-read"))) return limitedJson(c);
    if (!(await loadCurrent(reader))) return notConfiguredJson(c, NOT_PUBLISHED);
    const pageid = parsePageid(c.req.param("pageid"));
    if (pageid === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    // The records are the ones the article's view names (records_sha256), or the store is damaged:
    // missing, cut short, or uploaded ahead of their view.
    const article = await loadArticle(reader, pageid);
    if (!article) return c.json({ ok: false, error: "unknown pageid" }, 404);
    const text = await reader.text(`records/${pageid}.jsonl`);
    if (text === null || (await sha256Hex(text)) !== article.records_sha256) {
      throw new ArticlesStoreError(`articles records/${pageid}.jsonl is missing or not the records its view names`);
    }
    return serveStored(c, text, "application/x-ndjson; charset=utf-8");
  }));

  // Not immutable: the key is the producer's universe_sha256, which covers the
  // libraries' ids, commits and index shas but not the metadata stored beside
  // them (display_name, roots, toolchain, licence, exclusions), so a later
  // campaign on the same frozen universe may rewrite the object. A store holds one
  // universe, the one current.json names (the sets of a build share it).
  app.get("/api/articles/universe/:sha", (c) => storeGuard(c, true, { route: "universe" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    if (!(await readAllowed(c, "articles-meta"))) return limitedJson(c);
    const current = await loadCurrent(reader);
    if (!current) return notConfiguredJson(c, NOT_PUBLISHED);
    const sha = c.req.param("sha");
    if (!SHA_RE.test(sha) || sha !== current.universe?.sha256) return c.json({ ok: false, error: "unknown universe" }, 404);
    return serveCached(c, (await loadUniverseObject(reader, sha)).text, JSON_HEADERS["Cache-Control"], "unknown universe");
  }));

  app.get("/api/articles/index/:shard", (c) => storeGuard(c, true, { route: "index" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    if (!(await readAllowed(c, "articles-meta"))) return limitedJson(c);
    const current = await loadCurrent(reader);
    if (!current) return notConfiguredJson(c, NOT_PUBLISHED);
    const shard = c.req.param("shard");
    if (!SHARD_RE.test(shard) || !publishedShards(current).includes(shard)) {
      return c.json({ ok: false, error: "unknown shard" }, 404);
    }
    return serveCached(c, (await loadIndexShardObject(reader, shard)).text, JSON_HEADERS["Cache-Control"], "unknown shard");
  }));

  // ---- pages ----

  app.get("/articles", (c) => storeGuard(c, false, { route: "dashboard" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    const current = await loadCurrent(reader);
    if (!current) return notConfiguredHtml(c, NOT_PUBLISHED);
    const q = (c.req.query("q") ?? "").trim().slice(0, 200);
    if (q && !(await readAllowed(c, "articles-q"))) {
      return limitedHtml(c, "Too many searches", "Too many searches from this address in a minute.",
        "Try again shortly, or browse the list without a search.");
    }
    const universe = await loadUniverse(reader, current.universe.sha256);
    const shards = publishedShards(current);
    const loaded = await Promise.all(shards.map((s) => loadIndexShard(reader, s)));
    let rows: IndexEntry[] = loaded.flatMap((entries) => entries ?? []);
    const total = rows.length;
    if (q) {
      const needle = foldForSearch(q);
      rows = rows.filter((row) => entryKeys(row).folded.includes(needle));
    }
    const keyed = rows.map((row) => ({ row, key: entryKeys(row).key }));
    keyed.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : a.row.pageid - b.row.pageid));
    const shown: DashboardRow[] = keyed.slice(0, MAX_DASHBOARD_ROWS)
      .map(({ row }) => ({ ...row, href: articlesHref(row.pageid, row.title) }));
    return articlesHtml(c, articlesDashboardPage({ current, universe, q, rows: shown, matched: keyed.length, total, shards }));
  }));

  app.get("/articles/universe/:sha", (c) => storeGuard(c, false, { route: "universe-page" }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    const current = await loadCurrent(reader);
    if (!current) return notConfiguredHtml(c, NOT_PUBLISHED);
    const sha = c.req.param("sha");
    const universe = sha === current.universe?.sha256 ? await loadUniverse(reader, sha) : null;
    if (!universe) {
      return articlesHtml(c, articlesMessagePage("Unknown universe", "No published universe has that sha256.", ""), 404);
    }
    return articlesHtml(c, articlesUniversePage(universe));
  }));

  // An article view takes the read budget before it reads the store or D1.
  const articlePage = (c: Ctx) => storeGuard(c, false, { route: "article-page", pageid: c.req.param("pageid") }, async () => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredHtml(c);
    if (!(await readAllowed(c, "articles-read"))) {
      return limitedHtml(c, "Too many requests", "Too many requests from this address in a minute.",
        "Try again shortly.");
    }
    if (!(await loadCurrent(reader))) return notConfiguredHtml(c, NOT_PUBLISHED);
    const pageid = parsePageid(c.req.param("pageid"));
    const article = pageid === null ? null : await loadArticle(reader, pageid);
    if (!article) {
      return articlesHtml(c, articlesMessagePage("No entry",
        `There is no formalization entry for Wikipedia page id ${pageid === null ? "(invalid)" : pageid}.`,
        "Only articles a locate run has processed appear here."), 404);
    }
    const universe = await loadUniverse(reader, article.universe.sha256);
    const viewer = await getUser(c);
    const user = viewer && viewer.role !== "bot" && viewer.role !== "agent" ? viewer : null;
    const verdicts = user ? await viewerVerdicts(c.env, user.id, article.pageid, article.campaign) : new Map<string, string>();
    const wikileanSlug = await wikileanSlugFor(c.env, article.title);
    const selfHref = articlesHref(article.pageid, article.title);
    // The community overlay (plan section 2.8). ?overlay=off is the "As
    // published" view; the merged one is the default (decision 4). The sealed
    // view model served by /api/articles/article/:pageid is NOT merged — it
    // stays cacheable and hash-checkable; the merge happens here, per request,
    // and these pages send no Cache-Control, so no render key is bumped.
    const show = c.req.query("overlay") !== "off";
    const rows = await loadOverlay(c.env, article.pageid);
    const agreed = user ? (await loadAgreement(c.env, accountFor(user))) !== null : true;
    const overlay = {
      merged: mergeOverlay(article, rows, statusRuleOf(article)),
      show,
      canEdit: user !== null,
      needsAgreement: user !== null && !agreed,
      cc0Text: CC0_AGREEMENT_TEXT,
      cc0Version: CC0_AGREEMENT_VERSION,
      turnstileSiteKey: c.env.TURNSTILE_SITE_KEY || null,
      libraries: arrayOf(universe?.libraries).map((lib) => ({ id: lib.id, label: lib.display_name || lib.id })),
    };
    // With the store's marked revision (--reader-html) the page is the article itself, its
    // passages highlighted, and ?view=cards the card list; without it, the card list alone. The
    // card list reads no revision: it offers the switch when the view names one, and the reader
    // checks the bytes before it serves them (and is the card list when they are not the ones named).
    const cards = c.req.query("view") === "cards";
    const html = cards ? null : await loadReaderHtml(reader, article);
    const readerHref = (cards ? readerNamed(article) : html !== null) ? selfHref : null;
    const input = { article, universe, user, verdicts, wikileanSlug, selfHref, readerHref, overlay };
    const widget = pageNeedsTurnstile(overlay);
    if (html !== null) return articlesHtml(c, articlesReaderPage({ ...input, html }), 200, {}, widget);
    return articlesHtml(c, articlesArticlePage(input), 200, {}, widget);
  });
  app.get("/articles/:pageid{[0-9]+}", articlePage);
  app.get("/articles/:pageid{[0-9]+}/:slug", articlePage);

  // ---- feedback ----

  // Confirm, report or withdraw on one location. Session users only (the
  // pipeline bearer and agents are refused: these rows are human labels), same-origin
  // (checkOrigin), rate limited per user, byte-capped after those checks
  // (readJsonCapped, MAX_JSON_BODY_BYTES.articlesFeedback: 413 past it), and checked
  // against the current store so a key that is not on the page cannot be stored
  // (503 while the store has no published build, as every articles route answers).
  // Append-only: one row per verdict, never updated. What the person judged
  // (revision, campaign, record set, relation, pair key) is copied from the store.
  app.post("/api/articles/feedback", async (c) => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    const published = await storeRead(c, () => loadCurrent(reader));
    if (published instanceof Response) return published;
    if (!published) return notConfiguredJson(c, NOT_PUBLISHED);
    const originErr = checkOrigin(c);
    if (originErr) return originErr;
    const user = await getUser(c);
    if (!user) return c.json({ ok: false, error: "login required" }, 401);
    if (user.role === "bot" || user.role === "agent") {
      return c.json({ ok: false, error: "session required (no bots)" }, 403);
    }
    // The site-wide write brake covers every public write surface of this
    // plane, feedback included (site-settings.ts).
    const braked = await writeBrake(c);
    if (braked) return braked;
    const { success } = await c.env.EDIT_LIMITER.limit({ key: `articles:${user.id}` });
    if (!success) return c.json({ ok: false, error: "rate limited — slow down" }, 429);

    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.articlesFeedback);
    if (!read.ok) return read.response;
    const parsed = read.value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return c.json({ ok: false, error: "bad json" }, 400);
    const body = parsed as Record<string, unknown>;
    const { location_key: key, pageid, campaign, verdict } = body;
    if (typeof key !== "string" || !SHA_RE.test(key)) return c.json({ ok: false, error: "bad location_key" }, 400);
    if (typeof pageid !== "number" || !Number.isSafeInteger(pageid) || pageid < 1) {
      return c.json({ ok: false, error: "bad pageid" }, 400);
    }
    if (typeof campaign !== "string" || !CAMPAIGN_RE.test(campaign)) return c.json({ ok: false, error: "bad campaign" }, 400);
    if (typeof verdict !== "string" || !FEEDBACK_VERDICTS.has(verdict)) return c.json({ ok: false, error: "bad verdict" }, 400);
    let note: string | null = null;
    if (body.note !== undefined && body.note !== null) {
      if (typeof body.note !== "string" || body.note.length > MAX_NOTE_CHARS) {
        return c.json({ ok: false, error: `note must be a string of at most ${MAX_NOTE_CHARS} characters` }, 400);
      }
      note = body.note.trim() || null;
    }

    const article = await storeRead(c, () => loadArticle(reader, pageid));
    if (article instanceof Response) return article;
    if (!article) return c.json({ ok: false, error: "unknown pageid" }, 404);
    if (article.campaign !== campaign) {
      return c.json({ ok: false, error: "stale: these links changed since this page loaded", campaign: article.campaign }, 409);
    }
    const location = arrayOf(article.passages).flatMap((p) => arrayOf(p?.locations))
      .find((loc) => loc && typeof loc === "object" && loc.location_key === key);
    if (!location) return c.json({ ok: false, error: "unknown location_key" }, 404);

    const now = Date.now();
    try {
      await c.env.DB.prepare(
        `INSERT INTO atlas_feedback (location_key, pair_key, pageid, revision, campaign, record_set, relation, verdict, note, user_id, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).bind(
        key, location.pair_key ?? null, pageid, article.revision, article.campaign, article.record_set,
        location.relation, verdict, note, user.id, now,
      ).run();
    } catch (error) {
      if (missingTable(error)) return notConfiguredJson(c, "articles feedback not configured (migration 0015)");
      throw error;
    }
    console.log(JSON.stringify({ event: "articles-feedback", pageid, location_key: key, verdict, user_id: user.id, t: now }));
    return c.json({ ok: true, verdict });
  });

  // Benchmark seeding (docs/ARTICLES.md, "Feedback"): every verdict, id-ascending,
  // paged from D1 so the response never materializes in memory; a consumer takes
  // the newest row per (location_key, campaign, pseudonym), where "withdraw" means
  // none. Pipeline bearer only. Users appear only as pseudonyms, sha256(ARTICLES_PSEUDONYM_DOMAIN +
  // user_id + salt) truncated to 12 hex, with the research export's salt
  // (security.ts researchExportSalt: RESEARCH_EXPORT_SALT, else PIPELINE_TOKEN).
  // The domain prefix keeps them apart from the research export's
  // sha256(user_id + salt): this export carries free-text notes, so a note
  // that names its author must not re-identify that person's pseudonymous
  // annotation history there. Reads only D1, so it works whether or not ARTICLES
  // is bound.
  app.get("/api/articles/feedback/export", async (c) => {
    const user = await requireRole(c, ["bot"]);
    if (!user) return c.json({ ok: false, error: "forbidden" }, 403);
    const salt = researchExportSalt(c.env);
    const pseudonyms = new Map<string, string>();
    const pseudonymFor = async (userId: string) => {
      let value = pseudonyms.get(userId);
      if (value === undefined) {
        value = (await sha256Hex(ARTICLES_PSEUDONYM_DOMAIN + userId + salt)).slice(0, 12);
        pseudonyms.set(userId, value);
      }
      return value;
    };
    try {
      await c.env.DB.prepare("SELECT 1 FROM atlas_feedback LIMIT 1").all();
    } catch (error) {
      if (missingTable(error)) return notConfiguredJson(c, "articles feedback not configured (migration 0015)");
      throw error;
    }
    const PAGE = 500;
    let cursor = 0;
    const encoder = new TextEncoder();
    const env = c.env;
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        const { results } = await env.DB.prepare(
          `SELECT id, location_key, pair_key, pageid, revision, campaign, record_set, relation, verdict, note, provenance, user_id, created_at
           FROM atlas_feedback WHERE id > ? ORDER BY id LIMIT ?`,
        ).bind(cursor, PAGE).all<{
          id: number; location_key: string; pair_key: string | null; pageid: number; revision: number; campaign: string;
          record_set: string; relation: string; verdict: string; note: string | null; provenance: string;
          user_id: string; created_at: number;
        }>();
        if (results.length === 0) {
          controller.close();
          return;
        }
        cursor = results[results.length - 1].id;
        let out = "";
        for (const row of results) {
          out += JSON.stringify({
            feedback_id: row.id,
            location_key: row.location_key,
            pair_key: row.pair_key,
            pageid: row.pageid,
            revision: row.revision,
            campaign: row.campaign,
            record_set: row.record_set,
            relation: row.relation,
            verdict: row.verdict,
            note: row.note,
            provenance: row.provenance,
            pseudonym: await pseudonymFor(row.user_id),
            created_at: row.created_at,
          }) + "\n";
        }
        controller.enqueue(encoder.encode(out));
      },
    });
    return new Response(body, {
      headers: { "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store" },
    });
  });

  // ---- community tags ----

  // POST /api/articles/tag — add a Lean declaration to a passage, or change the
  // relation or note of a location already there (the pipeline's or an
  // editor's). One row per pair_key, last edit wins, under a CAS on the row's
  // own version. The server copies pageid, revision, campaign, record set and
  // universe from the store, and derives the repository from the article's own
  // universe: the client names only what it chose.
  app.post(TAG_PATH, async (c) => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    const published = await storeRead(c, () => loadCurrent(reader));
    if (published instanceof Response) return published;
    if (!published) return notConfiguredJson(c, NOT_PUBLISHED);
    const writer = await beginTagWrite(c);
    if (writer instanceof Response) return writer;

    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.articlesTag);
    if (!read.ok) return read.response;
    const parsed = read.value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return c.json({ ok: false, error: "bad json" }, 400);
    const body = parsed as Record<string, unknown>;
    const { pageid, passage_key: passageKey, library, declaration, relation, campaign } = body;
    if (typeof pageid !== "number" || !Number.isSafeInteger(pageid) || pageid < 1) {
      return c.json({ ok: false, error: "bad pageid" }, 400);
    }
    if (typeof passageKey !== "string" || !SHA_RE.test(passageKey)) return c.json({ ok: false, error: "bad passage_key" }, 400);
    if (typeof library !== "string" || !LIBRARY_ID_RE.test(library)) return c.json({ ok: false, error: "bad library" }, 400);
    if (typeof declaration !== "string" || !validDeclaration(declaration.trim())) {
      return c.json({ ok: false, error: `declaration must be a Lean name of at most ${MAX_DECLARATION_CHARS} characters` }, 400);
    }
    if (typeof relation !== "string" || !validRelation(relation)) {
      return c.json({ ok: false, error: `relation must be one of: ${TAG_RELATIONS.join(", ")}` }, 400);
    }
    if (typeof campaign !== "string" || !CAMPAIGN_RE.test(campaign)) return c.json({ ok: false, error: "bad campaign" }, 400);
    const note = optionalText(c, body.note, MAX_TAG_NOTE_CHARS, "note");
    if (note instanceof Response) return note;
    const baseVersion = body.base_version;
    if (baseVersion !== undefined && (typeof baseVersion !== "number" || !Number.isSafeInteger(baseVersion) || baseVersion < 0)) {
      return c.json({ ok: false, error: "bad base_version" }, 400);
    }
    // A bearer client must say what it read, as every bearer write here does.
    if (writer.actorType === "agent" && baseVersion === undefined) {
      return c.json({ ok: false, error: "base_version required" }, 400);
    }
    // model and run_id are the research export's, declared by an agent only.
    const model = writer.actorType === "agent" ? optionalText(c, body.model, 100, "model") : null;
    if (model instanceof Response) return model;
    const runId = writer.actorType === "agent" ? optionalText(c, body.run_id, 100, "run_id") : null;
    if (runId instanceof Response) return runId;

    const article = await storeRead(c, () => loadArticle(reader, pageid));
    if (article instanceof Response) return article;
    if (!article) return c.json({ ok: false, error: "unknown pageid" }, 404);
    if (article.campaign !== campaign) {
      return c.json({ ok: false, error: "stale: these links changed since this page loaded", campaign: article.campaign }, 409);
    }
    if (!hasPassageKeys(article)) return c.json({ ok: false, error: NO_PASSAGE_KEYS }, 422);
    const passage = arrayOf(article.passages).find((p) => p && p.passage_key === passageKey);
    if (!passage) return c.json({ ok: false, error: "unknown passage_key" }, 404);
    const universe = await storeRead(c, () => loadUniverse(reader, article.universe.sha256));
    if (universe instanceof Response) return universe;
    const entry = arrayOf(universe?.libraries).find((lib) => lib && lib.id === library);
    if (!entry || typeof entry.repo !== "string" || entry.repo === "") {
      return c.json({ ok: false, error: "unknown library for this article's universe" }, 400);
    }

    const decl = declaration.trim();
    // The producer's own key, so a tag of a declaration the pipeline already
    // located on this passage IS that location's row.
    const key = await pairKey(passageKey, entry.repo, decl);
    const now = Date.now();
    let existing: ArticleTag | null;
    try {
      existing = await loadTag(c.env, key);
    } catch (error) {
      const missing = tagMissingTable(c, error);
      if (missing) return missing;
      throw error;
    }
    if (existing && writer.actorType === "agent" && existing.actor_type === "human" && existing.state === "live") {
      return c.json({ ok: false, error: AGENT_HUMAN_ROW_REFUSED }, 403);
    }
    const current = existing?.version ?? 0;
    if (baseVersion !== undefined && baseVersion !== current) {
      return c.json({ ok: false, error: "stale", version: current, tag: existing }, 409);
    }
    const agreement = await requireAgreement(c, writer, body, now);
    if (agreement instanceof Response) return agreement;

    const base = locationAt(article, key);
    const baseRelation = base ? base.loc.relation : null;
    const writes: D1PreparedStatement[] = [
      c.env.DB.prepare(
        `INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, note,
            state, origin, base_relation, added_by, actor_type, via, token_id, model, run_id, campaign, revision,
            record_set, universe_sha256, version, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', 'community', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1, ?, ?)
         ON CONFLICT(pair_key) DO UPDATE SET
            relation = excluded.relation, note = excluded.note, state = 'live',
            base_relation = excluded.base_relation, added_by = excluded.added_by,
            actor_type = excluded.actor_type, via = excluded.via, model = excluded.model, run_id = excluded.run_id,
            campaign = excluded.campaign, revision = excluded.revision, record_set = excluded.record_set,
            universe_sha256 = excluded.universe_sha256, version = article_tags.version + 1,
            updated_at = excluded.updated_at, removed_by = NULL, removed_at = NULL, removed_reason = NULL
         WHERE article_tags.version = ?`,
      ).bind(
        key, pageid, passageKey, library, entry.repo, decl, relation, note,
        baseRelation, writer.user.id, writer.actorType, writer.via, model, runId, article.campaign,
        article.revision, article.record_set, article.universe.sha256, now, now, current,
      ),
    ];
    if (agreement.needsRecord) {
      writes.push(c.env.DB.prepare(
        "INSERT INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(user_id) DO NOTHING",
      ).bind(writer.account, CC0_AGREEMENT_VERSION, now, agreement.verifiedAt));
    }
    let changes = 0;
    try {
      const results = await c.env.DB.batch(writes);
      changes = results[0]?.meta?.changes ?? 0;
    } catch (error) {
      const missing = tagMissingTable(c, error);
      if (missing) return missing;
      throw error;
    }
    if (changes === 0) {
      // The CAS lost: somebody else saved between the read and the write.
      const fresh = await loadTag(c.env, key);
      return c.json({ ok: false, error: "stale", version: fresh?.version ?? 0, tag: fresh }, 409);
    }
    const version = current + 1;
    console.log(JSON.stringify({
      event: "articles-tag", pageid, pair_key: key, relation, actor_type: writer.actorType,
      user_id: writer.user.id, version, t: now,
    }));
    return c.json({
      ok: true, pair_key: key, version, state: "live", relation,
      confirms: baseRelation === relation && (base?.loc.note ?? null) === note,
      status: await statusAfter(c.env, article, passage),
    }, 200, { "Cache-Control": "no-store" });
  });

  // POST /api/articles/tag/:pair_key/state — "this link is wrong" (a SOFT
  // delete that leaves a gravestone: state, remover, time and a required
  // reason), and its undo. Removing a location the pipeline made writes the
  // gravestone row for it; the sealed record is untouched, so restoring it is
  // always safe. An admin reverts an edit by writing the row back.
  app.post(`${TAG_PATH}/:pair_key/state`, async (c) => {
    const reader = articlesReader(c.env);
    if (!reader) return notConfiguredJson(c);
    const published = await storeRead(c, () => loadCurrent(reader));
    if (published instanceof Response) return published;
    if (!published) return notConfiguredJson(c, NOT_PUBLISHED);
    const key = c.req.param("pair_key");
    if (!SHA_RE.test(key)) return c.json({ ok: false, error: "bad pair_key" }, 400);
    const writer = await beginTagWrite(c);
    if (writer instanceof Response) return writer;

    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.articlesTag);
    if (!read.ok) return read.response;
    const parsed = read.value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return c.json({ ok: false, error: "bad json" }, 400);
    const body = parsed as Record<string, unknown>;
    const { pageid, state, campaign } = body;
    if (typeof pageid !== "number" || !Number.isSafeInteger(pageid) || pageid < 1) {
      return c.json({ ok: false, error: "bad pageid" }, 400);
    }
    if (state !== "live" && state !== "removed") return c.json({ ok: false, error: "state must be 'live' or 'removed'" }, 400);
    if (typeof campaign !== "string" || !CAMPAIGN_RE.test(campaign)) return c.json({ ok: false, error: "bad campaign" }, 400);
    const reason = optionalText(c, body.reason, MAX_REMOVE_REASON_CHARS, "reason");
    if (reason instanceof Response) return reason;
    if (state === "removed" && !reason) return c.json({ ok: false, error: "a reason is required to remove a link" }, 400);
    const baseVersion = body.base_version;
    if (baseVersion !== undefined && (typeof baseVersion !== "number" || !Number.isSafeInteger(baseVersion) || baseVersion < 0)) {
      return c.json({ ok: false, error: "bad base_version" }, 400);
    }
    if (writer.actorType === "agent" && baseVersion === undefined) {
      return c.json({ ok: false, error: "base_version required" }, 400);
    }

    const article = await storeRead(c, () => loadArticle(reader, pageid));
    if (article instanceof Response) return article;
    if (!article) return c.json({ ok: false, error: "unknown pageid" }, 404);
    if (article.campaign !== campaign) {
      return c.json({ ok: false, error: "stale: these links changed since this page loaded", campaign: article.campaign }, 409);
    }
    let existing: ArticleTag | null;
    try {
      existing = await loadTag(c.env, key);
    } catch (error) {
      const missing = tagMissingTable(c, error);
      if (missing) return missing;
      throw error;
    }
    if (existing && existing.pageid !== pageid) return c.json({ ok: false, error: "unknown pair_key" }, 404);
    const base = locationAt(article, key);
    if (!existing && !base) {
      if (!hasPassageKeys(article)) return c.json({ ok: false, error: NO_PASSAGE_KEYS }, 422);
      return c.json({ ok: false, error: "unknown pair_key" }, 404);
    }
    if (existing && writer.actorType === "agent" && existing.actor_type === "human" && existing.state === "live") {
      return c.json({ ok: false, error: AGENT_HUMAN_ROW_REFUSED }, 403);
    }
    const current = existing?.version ?? 0;
    if (baseVersion !== undefined && baseVersion !== current) {
      return c.json({ ok: false, error: "stale", version: current, tag: existing }, 409);
    }
    const passage = existing
      ? arrayOf(article.passages).find((p) => p && p.passage_key === existing!.passage_key) ?? base?.passage
      : base?.passage;
    if (!passage) return c.json({ ok: false, error: "unknown pair_key" }, 404);
    const agreement = await requireAgreement(c, writer, body, Date.now());
    if (agreement instanceof Response) return agreement;

    const now = Date.now();
    const removed = state === "removed";
    const writes: D1PreparedStatement[] = [];
    if (existing) {
      writes.push(c.env.DB.prepare(
        `UPDATE article_tags
            SET state = ?, version = version + 1, updated_at = ?,
                removed_by = ?, removed_at = ?, removed_reason = ?
          WHERE pair_key = ? AND version = ?`,
      ).bind(state, now, removed ? writer.user.id : null, removed ? now : null, removed ? reason : null, key, current));
    } else {
      // No community row yet: the gravestone (or the restore, a no-op that
      // records agreement with the pipeline) is written from the store's own
      // location, copied here exactly as the add route copies it.
      const loc = base!.loc;
      writes.push(c.env.DB.prepare(
        `INSERT INTO article_tags (pair_key, pageid, passage_key, library, repository, declaration, relation, note,
            state, origin, base_relation, added_by, actor_type, via, token_id, model, run_id, campaign, revision,
            record_set, universe_sha256, version, created_at, updated_at, removed_by, removed_at, removed_reason)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'community', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
         ON CONFLICT(pair_key) DO NOTHING`,
      ).bind(
        key, pageid, String(passage.passage_key ?? ""), loc.library, loc.repo, loc.declaration, loc.relation, loc.note,
        state, loc.relation, writer.user.id, writer.actorType, writer.via, article.campaign, article.revision,
        article.record_set, article.universe.sha256, now, now,
        removed ? writer.user.id : null, removed ? now : null, removed ? reason : null,
      ));
    }
    if (agreement.needsRecord) {
      writes.push(c.env.DB.prepare(
        "INSERT INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(user_id) DO NOTHING",
      ).bind(writer.account, CC0_AGREEMENT_VERSION, now, agreement.verifiedAt));
    }
    let changes = 0;
    try {
      const results = await c.env.DB.batch(writes);
      changes = results[0]?.meta?.changes ?? 0;
    } catch (error) {
      const missing = tagMissingTable(c, error);
      if (missing) return missing;
      throw error;
    }
    if (changes === 0) {
      const fresh = await loadTag(c.env, key);
      return c.json({ ok: false, error: "stale", version: fresh?.version ?? 0, tag: fresh }, 409);
    }
    console.log(JSON.stringify({
      event: "articles-tag-state", pageid, pair_key: key, state, actor_type: writer.actorType,
      user_id: writer.user.id, t: now,
    }));
    return c.json({
      ok: true, pair_key: key, version: current + 1, state,
      status: await statusAfter(c.env, article, passage),
    }, 200, { "Cache-Control": "no-store" });
  });

  // GET /api/articles/overlay/:pageid — one article's community overlay, never
  // cached. Deliberately NOT merged into /api/articles/article/:pageid, which
  // stays the sealed view model verbatim, cacheable and hash-checkable
  // (public, max-age=300). Reads only D1, so it answers whether or not the
  // store is bound; anonymous reads take the plane's small-object budget.
  app.get("/api/articles/overlay/:pageid", async (c) => {
    if (!(await readAllowed(c, "articles-meta"))) return limitedJson(c);
    const pageid = parsePageid(c.req.param("pageid"));
    if (pageid === null) return c.json({ ok: false, error: "unknown pageid" }, 404);
    const overlay = await loadOverlay(c.env, pageid);
    return c.json({
      ok: true,
      pageid,
      tags: overlay.tags.map((tag) => {
        const author = overlay.authors.get(tag.added_by) ?? null;
        return {
          pair_key: tag.pair_key, passage_key: tag.passage_key, library: tag.library, repository: tag.repository,
          declaration: tag.declaration, relation: tag.relation, note: tag.note, state: tag.state,
          base_relation: tag.base_relation, actor_type: tag.actor_type, via: tag.via,
          model: tag.model, run_id: tag.run_id, campaign: tag.campaign, revision: tag.revision,
          version: tag.version, created_at: tag.created_at, updated_at: tag.updated_at,
          removed_at: tag.removed_at, removed_reason: tag.removed_reason,
          added_by: { id: tag.added_by, name: author?.name ?? tag.added_by, owner_id: author?.ownerId ?? null, owner_name: author?.ownerName ?? null },
          removed_by: tag.removed_by,
        };
      }),
    }, 200, { "Cache-Control": "no-store" });
  });
}
