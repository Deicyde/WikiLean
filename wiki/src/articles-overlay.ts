// The community overlay over the sealed formalization store: the data layer of
// "tag this passage" (release R1 of the public-editing plan; docs/ARTICLES.md,
// "Community tags"). The routes are in articles.ts; the cards are in
// articles-render.ts; this module holds the keys, the D1 reads, the merge and
// the policy checks that both sides share.
//
// The rule the whole design rests on: **the sealed record set is never
// written.** A community tag is a row in D1 keyed by the producer's own
// revision- and commit-free pair_key, so a tag of the declaration the pipeline
// already located on that passage is the SAME ROW as the pipeline's location —
// agreement, correction and removal share one key and there is no re-pairing
// problem. `/api/articles/records/:pageid` still serves the sealed bytes and
// still passes its own hash check; the overlay is a separate table the nightly
// cannot see.
//
// Correction is the Brain overlay's model (brain_edges, brain_nodes): live on
// save, attributed, human/agent labelled, and a soft delete that leaves a
// gravestone (removed_by / removed_at / removed_reason). R1 has no history,
// diff or revert — an admin reverts by writing the row back. Release R2 adds
// article_overlay_revisions.
//
// Imports are one-way: this module takes runtime values from articles-render.ts
// (which only type-imports articles.ts) and types from articles.ts, so there is
// no import cycle with either.

import type { Context } from "hono";
import type { Env } from "./env.js";
import type { AuthUser } from "./auth.js";
import type { ArticlesArticle, ArticlesLocation, ArticlesPassage, SummaryStatus } from "./articles.js";
import {
  BUNDLE_PART_RELATION, coverageStatus as bundleStandInStatus, locationStatus,
  MAX_BUNDLE_PARTS, MIN_BUNDLE_PARTS, passageStatus,
} from "./articles-render.js";
export { BUNDLE_PART_RELATION, MAX_BUNDLE_PARTS, MIN_BUNDLE_PARTS } from "./articles-render.js";
// The brake lives in a module of its own so the Brain's write paths can read
// it without pulling in the articles plane; re-exported here for the routes.
export { BRAKE_MESSAGE, SITE_WRITE_MODE_KEY, siteWriteMode, WRITE_MODE_OPEN, writeBrake } from "./site-settings.js";

// ---- vocabulary ------------------------------------------------------------

// scripts/pipeline/vocab.py LOCATION_RELATIONS, verbatim: a community row and a
// pipeline location are then comparable rather than free text.
export const TAG_RELATIONS: readonly string[] = [
  "exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch",
];
const RELATION_SET = new Set(TAG_RELATIONS);

// A coverage bundle's own relation is its weakest part's, and a part states its
// clause strongly: the three strong relations and no other
// (build_store.BUNDLE_RELATIONS, verbatim).
export const BUNDLE_RELATIONS: readonly string[] = ["exactMatch", "closeMatch", "broadMatch"];
const BUNDLE_RELATION_SET = new Set(BUNDLE_RELATIONS);

// Mirrors articles.ts SUMMARY_STATUSES, best first. Kept local so this module
// needs no runtime import from articles.ts, which would make an import cycle
// (test/articles-tag.test.ts pins the two lists against each other).
const STATUS_RANK: Record<string, number> = {
  formalized: 0, statement_only: 1, partial: 2, not_formalized: 3, not_checked: 4, unresolved: 5,
};
export const OVERLAY_STATUSES: readonly string[] = ["formalized", "statement_only", "partial", "not_formalized", "not_checked", "unresolved"];

export const MAX_DECLARATION_CHARS = 240;
export const MAX_TAG_NOTE_CHARS = 500;
export const MAX_REMOVE_REASON_CHARS = 500;
// A bundle's clauses: 2 to 5 of them (build_store MIN_CONJUNCTS / MAX_CONJUNCTS),
// each a plain sentence held to the tag note's cap rather than the producer's
// 1,000 characters — the overlay's free text is all held to one length.
export const MIN_CONJUNCTS = 2;
export const MAX_CONJUNCTS = 5;
export const MAX_CONJUNCT_CHARS = MAX_TAG_NOTE_CHARS;

// Lean's name grammar, as much of it as a tag needs: one or more components
// separated by single dots, with no whitespace anywhere and no empty component
// (so no leading, trailing or doubled dot). Deliberately permissive about what
// a component may CONTAIN — Lean allows digits, primes and subscripts and
// WikiLean has no business ruling them out — and deliberately strict about
// whitespace, which rules out Lean's «guillemet» names: in a formalization link
// a space is a typo or an injection attempt far more often than it is
// «a name like this». Existence is a SOFT check (the declaration index is
// Mathlib-only today) and never a refusal; shape is the only hard rule.
const DECLARATION_RE = /^[^\s.]+(?:\.[^\s.]+)*$/u;
const CONTROL_RE = /[\p{C}\p{Zl}\p{Zp}]/u;

export function validDeclaration(name: string): boolean {
  return name.length >= 1 && name.length <= MAX_DECLARATION_CHARS
    && !CONTROL_RE.test(name) && DECLARATION_RE.test(name);
}

export function validRelation(relation: string): boolean {
  return RELATION_SET.has(relation);
}

export function validBundleRelation(relation: string): boolean {
  return BUNDLE_RELATION_SET.has(relation);
}

// A conjunct's text: one plain clause. Bounded like a tag note, and with no
// control characters — the page writes it with textContent, and a line
// separator in the middle of a clause is a paste accident, never a sentence.
export function validConjunctText(value: string): boolean {
  return value.length >= 1 && value.length <= MAX_CONJUNCT_CHARS && !CONTROL_RE.test(value);
}

// ---- keys ------------------------------------------------------------------

async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// scripts/articles/build_store.py pair_key: sha256("<passage_key>\n<repository>\n<declaration>"),
// the repository being the Lean end's URI (NOT the library id — two libraries of
// one repository, such as lean-core's Init and Std, share a repository, which is
// why the row carries its library id as a column beside the key).
export function pairKey(passageKey: string, repository: string, declaration: string): Promise<string> {
  return sha256Hex(`${passageKey}\n${repository}\n${declaration}`);
}

// scripts/articles/build_store.py bundle_key (cflib.hashing.bundle_key): the
// passage key and the parts' pair keys, sorted, one per line. Sorted, so the
// same parts named in any order are the same bundle and the same row.
export function bundleKey(passageKey: string, pairKeys: readonly string[]): Promise<string> {
  return sha256Hex([passageKey, ...[...pairKeys].sort()].join("\n"));
}

// ---- rows ------------------------------------------------------------------

export type TagState = "live" | "removed";
export type TagActor = "human" | "agent";

export interface ArticleTag {
  pair_key: string;
  pageid: number;
  passage_key: string;
  library: string;
  repository: string;
  declaration: string;
  relation: string;
  note: string | null;
  state: TagState;
  origin: string;
  base_relation: string | null;
  added_by: string;
  actor_type: TagActor;
  via: string;
  token_id: string | null;
  model: string | null;
  run_id: string | null;
  campaign: string;
  revision: number;
  record_set: string;
  universe_sha256: string;
  version: number;
  created_at: number;
  updated_at: number;
  removed_by: string | null;
  removed_at: number | null;
  removed_reason: string | null;
}

const TAG_COLUMNS =
  "pair_key, pageid, passage_key, library, repository, declaration, relation, note, state, origin, " +
  "base_relation, added_by, actor_type, via, token_id, model, run_id, campaign, revision, record_set, " +
  "universe_sha256, version, created_at, updated_at, removed_by, removed_at, removed_reason";

// One community coverage bundle (migration 0021; issue #173): the overlay's
// mirror of the sealed store's claim, saying that these 2 to 5 locations of one
// passage together state every clause of it. `parts` and `conjuncts` are the
// JSON text D1 holds; read them through bundleParts / bundleConjuncts, which
// never throw.
export interface ArticleBundle {
  bundle_key: string;
  pageid: number;
  passage_key: string;
  parts: string;
  conjuncts: string;
  relation: string;
  state: TagState;
  origin: string;
  added_by: string;
  actor_type: TagActor;
  via: string;
  token_id: string | null;
  model: string | null;
  run_id: string | null;
  campaign: string;
  revision: number;
  record_set: string;
  universe_sha256: string;
  version: number;
  created_at: number;
  updated_at: number;
  removed_by: string | null;
  removed_at: number | null;
  removed_reason: string | null;
}

export interface BundleConjunct {
  id: string;
  text: string;
  part: string;
}

const BUNDLE_COLUMNS =
  "bundle_key, pageid, passage_key, parts, conjuncts, relation, state, origin, added_by, actor_type, via, " +
  "token_id, model, run_id, campaign, revision, record_set, universe_sha256, version, created_at, updated_at, " +
  "removed_by, removed_at, removed_reason";

// The parts a stored bundle names. A row whose JSON is not a list of strings
// reads as no parts, so it names no live location and the bundle counts for
// nothing: a damaged row is inert, never an exception on an article page.
export function bundleParts(bundle: Pick<ArticleBundle, "parts">): string[] {
  try {
    const value: unknown = JSON.parse(bundle.parts);
    if (!Array.isArray(value) || !value.every((key) => typeof key === "string")) return [];
    return value as string[];
  } catch {
    return [];
  }
}

// The clauses a stored bundle names, each beside the part stating it. Malformed
// entries are dropped; a row with none shows its parts and no clauses.
export function bundleConjuncts(bundle: Pick<ArticleBundle, "conjuncts">): BundleConjunct[] {
  try {
    const value: unknown = JSON.parse(bundle.conjuncts);
    if (!Array.isArray(value)) return [];
    return value.filter((c): c is BundleConjunct =>
      !!c && typeof c === "object" && typeof (c as BundleConjunct).id === "string"
      && typeof (c as BundleConjunct).text === "string" && typeof (c as BundleConjunct).part === "string");
  } catch {
    return [];
  }
}

// A D1 error naming a table 0019 or 0021 creates, unwrapped through the cause
// chain the way articles.ts missingTable does for atlas_feedback. Until the
// migration is applied, the overlay reads as empty and the write routes answer
// 503 rather than 500.
export function overlayTableMissing(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const message = current instanceof Error ? current.message : String(current);
    if (/no such table: (article_tags|article_coverage|site_settings|user_agreements)\b/.test(message)) return true;
    current = typeof current === "object" && current !== null && "cause" in current ? (current as { cause?: unknown }).cause : null;
  }
  return false;
}

// 0021 alone, so a deployment that has 0019 but not 0021 keeps every tag route
// working and refuses only the two coverage routes (503).
export function coverageTableMissing(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const message = current instanceof Error ? current.message : String(current);
    if (/no such table: article_coverage\b/.test(message)) return true;
    current = typeof current === "object" && current !== null && "cause" in current ? (current as { cause?: unknown }).cause : null;
  }
  return false;
}

// ---- who wrote a row -------------------------------------------------------

export interface TagAuthor {
  id: string;
  name: string;
  actor: TagActor;
  // role 'agent' only: the person accountable for it (PR #126's
  // agent_tokens.owner_id). Every agent names a human, and the card says so.
  ownerId: string | null;
  ownerName: string | null;
}

interface AuthorRow {
  id: string;
  name: string | null;
  role: string;
  owner_id: string | null;
  owner_name: string | null;
}

async function loadAuthors(env: Pick<Env, "DB">, ids: string[]): Promise<Map<string, TagAuthor>> {
  const out = new Map<string, TagAuthor>();
  if (ids.length === 0) return out;
  const unique = [...new Set(ids)].slice(0, 200);
  const marks = unique.map(() => "?").join(",");
  let results: AuthorRow[] = [];
  try {
    ({ results } = await env.DB.prepare(
      `SELECT u.id AS id, u.name AS name, u.role AS role, t.owner_id AS owner_id, o.name AS owner_name
         FROM users u
         LEFT JOIN agent_tokens t ON t.agent_id = u.id
         LEFT JOIN users o ON o.id = t.owner_id
        WHERE u.id IN (${marks})`,
    ).bind(...unique).all<AuthorRow>());
  } catch (error) {
    // agent_tokens is migration 0017's; without it the owner is simply unknown.
    if (!/no such table: agent_tokens/.test(String(error))) throw error;
    ({ results } = await env.DB.prepare(
      `SELECT id, name, role, NULL AS owner_id, NULL AS owner_name FROM users WHERE id IN (${marks})`,
    ).bind(...unique).all<AuthorRow>());
  }
  for (const row of results) {
    out.set(row.id, {
      id: row.id,
      name: row.name ?? row.id,
      actor: row.role === "agent" ? "agent" : "human",
      ownerId: row.owner_id ?? null,
      ownerName: row.owner_name ?? null,
    });
  }
  return out;
}

// ---- reading one article's overlay -----------------------------------------

export interface ArticleOverlay {
  tags: ArticleTag[];
  bundles: ArticleBundle[];
  authors: Map<string, TagAuthor>;
}

export const EMPTY_OVERLAY: ArticleOverlay = { tags: [], bundles: [], authors: new Map() };

// The store's status rule (a /2 seal names 2 or 3; a /1 store names none and
// reads as 1), the number location_status and the merge are computed under.
// Mirrors articles-render.ts articleParts.
export function statusRuleOf(article: Pick<ArticlesArticle, "seal">): number {
  const seal = article.seal && typeof article.seal === "object" ? article.seal : null;
  return seal && Number.isSafeInteger(seal.status_rule) ? (seal.status_rule as number) : 1;
}

// At most this many overlay rows are read for one article. Batch 1's largest
// article has about 90 passages; a cap keeps one article's page bounded whatever
// the overlay grows to, and the page says when it is reached.
export const MAX_OVERLAY_ROWS = 500;

// Every community row on one article, newest first. Empty (never an error) when
// migration 0019 is not applied, so an unapplied migration shows the sealed
// article rather than a 500.
export async function loadOverlayTags(env: Pick<Env, "DB">, pageid: number): Promise<ArticleTag[]> {
  try {
    const { results } = await env.DB.prepare(
      `SELECT ${TAG_COLUMNS} FROM article_tags WHERE pageid = ? ORDER BY created_at DESC, pair_key LIMIT ?`,
    ).bind(pageid, MAX_OVERLAY_ROWS).all<ArticleTag>();
    return results;
  } catch (error) {
    if (!overlayTableMissing(error)) throw error;
    return [];
  }
}

// Every community coverage bundle on one article, newest first. Empty (never an
// error) when migration 0021 is not applied, so a deployment that has the tag
// tables and not this one shows its article exactly as before.
export async function loadOverlayBundles(env: Pick<Env, "DB">, pageid: number): Promise<ArticleBundle[]> {
  try {
    const { results } = await env.DB.prepare(
      `SELECT ${BUNDLE_COLUMNS} FROM article_coverage WHERE pageid = ? ORDER BY created_at DESC, bundle_key LIMIT ?`,
    ).bind(pageid, MAX_OVERLAY_ROWS).all<ArticleBundle>();
    return results;
  } catch (error) {
    if (!overlayTableMissing(error)) throw error;
    return [];
  }
}

// The same rows with the people and agents that wrote them, for the cards.
export async function loadOverlay(env: Pick<Env, "DB">, pageid: number): Promise<ArticleOverlay> {
  const [tags, bundles] = await Promise.all([loadOverlayTags(env, pageid), loadOverlayBundles(env, pageid)]);
  if (tags.length === 0 && bundles.length === 0) return EMPTY_OVERLAY;
  const authors = await loadAuthors(env, [...tags.map((t) => t.added_by), ...bundles.map((b) => b.added_by)]);
  return { tags, bundles, authors };
}

export async function loadTag(env: Pick<Env, "DB">, key: string): Promise<ArticleTag | null> {
  const { results } = await env.DB.prepare(
    `SELECT ${TAG_COLUMNS} FROM article_tags WHERE pair_key = ?`,
  ).bind(key).all<ArticleTag>();
  return results[0] ?? null;
}

export async function loadBundle(env: Pick<Env, "DB">, key: string): Promise<ArticleBundle | null> {
  const { results } = await env.DB.prepare(
    `SELECT ${BUNDLE_COLUMNS} FROM article_coverage WHERE bundle_key = ?`,
  ).bind(key).all<ArticleBundle>();
  return results[0] ?? null;
}

// ---- the merge -------------------------------------------------------------

export type CardOrigin = "pipeline" | "community";

export interface MergedLocation {
  // What the reader shows. For a changed pipeline location this is the
  // pipeline's location with the community's relation and note applied, so the
  // card renders exactly as any other card does.
  loc: ArticlesLocation;
  origin: CardOrigin;
  state: TagState;
  tag: ArticleTag | null;
  author: TagAuthor | null;
  // The pipeline's own value, when a community row overrode it.
  baseRelation: string | null;
  baseNote: string | null;
  // The community row agrees with the pipeline (same relation, no note change).
  confirms: boolean;
}

// One community coverage bundle as the reader shows it, and as the merge counts
// it: its row, its parts resolved against the merged locations, and the status
// it gives the passage on its own (null when it gives none).
export interface MergedBundle {
  bundle: ArticleBundle;
  parts: string[];
  conjuncts: BundleConjunct[];
  // The merged locations its parts name, in the bundle's own order. A part the
  // merge does not show as a live location is a null, which withdraws the
  // bundle: `counts` is then false and the card says why.
  locations: Array<MergedLocation | null>;
  state: TagState;
  author: TagAuthor | null;
  // Every part is a live merged location whose relation, after any community
  // override, is hasPart — the one shape in which a bundle counts.
  counts: boolean;
  status: SummaryStatus | null;
}

export interface MergedPassage {
  locations: MergedLocation[];
  // Community coverage bundles on this passage, newest first, live and
  // gravestones alike.
  bundles: MergedBundle[];
  status: SummaryStatus;
  sealedStatus: SummaryStatus;
  // Any community row touches this passage (live or a gravestone).
  edited: boolean;
}

export interface MergedArticle {
  byPassage: Map<string, MergedPassage>;
  // Community rows on this article: live ones, gravestones, and the total.
  live: number;
  removed: number;
  edits: number;
  // The merged by_status counts over the passages the page shows.
  byStatus: Record<SummaryStatus, number>;
  truncated: boolean;
}

function asStatus(value: unknown): SummaryStatus {
  return (OVERLAY_STATUSES as string[]).includes(String(value)) ? (value as SummaryStatus) : "unresolved";
}

function list<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

// A community row shown as a location card. The fields the store would carry but
// a community tag does not know (module, path, line, rank, confidence,
// challenge, signature) are left empty or neutral: the card shows the
// declaration's name, its library and its relation, and nothing it cannot stand
// behind.
//
// `proof_status` is the one that needs a decision, because the derived status
// reads it. §2.2's write surface asks the editor for a declaration and a
// relation, not for whether Lean's proof is finished, and the server cannot
// know. Claiming `proved` would make a community exactMatch *formalized* — the
// strongest thing this site says, on the word of one editor and no evidence.
// So a community location is `statement_only`, the conservative reading: the
// passage's status still MOVES (an exactMatch on an absence becomes "statement
// only", not "not formalized"), which is what makes editing worth doing, but it
// never mints a formalization claim. Release R2's history and R3's patrol queue
// are where a reviewer confirms more than that.
const COMMUNITY_PROOF_STATUS = "statement_only";

function communityLocation(tag: ArticleTag, repoCommit: string): ArticlesLocation {
  return {
    library: tag.library,
    repo: tag.repository,
    commit: repoCommit,
    declaration: tag.declaration,
    module: "",
    path: "",
    line: 0,
    relation: tag.relation,
    proof_status: COMMUNITY_PROOF_STATUS,
    rank: 0,
    confidence: "community",
    challenge: "none",
    location_key: tag.pair_key,
    pair_key: tag.pair_key,
    note: tag.note,
  };
}

// The store's survived coverage claim still counts when every part it names is
// still a live location after the merge; a community removal of one part
// withdraws it (build_store.coverage_status reads the parts, so a withdrawn part
// is no longer a claim the overlay may lean on).
function coverageStatus(passage: ArticlesPassage, liveKeys: Set<string>): SummaryStatus | null {
  const claim = passage.coverage;
  if (!claim || claim.challenge !== "survived" || typeof claim.status !== "string") return null;
  const parts = list(claim.parts);
  if (parts.length < 2 || !parts.every((key) => liveKeys.has(key))) return null;
  return asStatus(claim.status);
}

// The passage's derived status over the merged set, by the same rules the store
// uses (articles-render locationStatus, a port of build_store.location_status) so
// a community exactMatch on a passage the pipeline left not_formalized moves the
// article's headline. A passage the locate run left unresolved is set aside by
// both views and the overlay never moves it: nothing offers a tag on a passage
// no view lists.
export function mergedStatus(
  passage: ArticlesPassage,
  live: ArticlesLocation[],
  statusRule: number,
  // The statuses the passage's live community bundles give on their own
  // (mergeOverlay computes them; a bundle that does not count gives null).
  bundleStatuses: ReadonlyArray<SummaryStatus | null> = [],
): SummaryStatus {
  const sealed = passageStatus(passage);
  if (sealed === "unresolved") return sealed;
  let best: SummaryStatus | null = null;
  for (const loc of live) {
    const status = locationStatus(String(passage.kind ?? ""), loc, statusRule);
    if (status && (best === null || STATUS_RANK[status] < STATUS_RANK[best])) best = status;
  }
  const claimed = coverageStatus(passage, new Set(live.map((loc) => loc.location_key)));
  if (claimed && (best === null || STATUS_RANK[claimed] < STATUS_RANK[best])) best = claimed;
  // A community bundle is one more location, by the builder's own stand-in
  // (articles-render coverageStatus, issue #173). It never mints "formalized"
  // on a community part's word: such a part is statement_only, so a bundle
  // holding one is unfinished and gives statement only at best.
  for (const bundled of bundleStatuses) {
    if (bundled && (best === null || STATUS_RANK[bundled] < STATUS_RANK[best])) best = bundled;
  }
  // Nothing in the universe states it any more: the editors removed every
  // location that counted. "Not formalized" is what that means, and it is the
  // status an absence carries — but only where something was searched. A statement no
  // library has been searched for stays not_checked, whatever the editors do with it:
  // the overlay may give it a Lean declaration, never a claim that there is none.
  return best ?? (sealed === "not_checked" ? "not_checked" : "not_formalized");
}

function emptyCounts(): Record<SummaryStatus, number> {
  return { formalized: 0, statement_only: 0, partial: 0, not_formalized: 0, not_checked: 0, unresolved: 0 };
}

// Merge one article's sealed view model with its overlay rows. The sealed view
// model is never mutated: every changed location is a copy.
export function mergeOverlay(
  article: ArticlesArticle,
  overlay: ArticleOverlay,
  statusRule: number,
): MergedArticle {
  const byKey = new Map<string, ArticleTag>();
  for (const tag of overlay.tags) byKey.set(tag.pair_key, tag);
  const byPassageKey = new Map<string, ArticleTag[]>();
  for (const tag of overlay.tags) {
    const bucket = byPassageKey.get(tag.passage_key);
    if (bucket) bucket.push(tag);
    else byPassageKey.set(tag.passage_key, [tag]);
  }
  const bundlesByPassage = new Map<string, ArticleBundle[]>();
  for (const bundle of overlay.bundles) {
    const bucket = bundlesByPassage.get(bundle.passage_key);
    if (bucket) bucket.push(bundle);
    else bundlesByPassage.set(bundle.passage_key, [bundle]);
  }
  const byPassage = new Map<string, MergedPassage>();
  const byStatus = emptyCounts();
  let live = 0;
  let removed = 0;
  for (const row of [...overlay.tags, ...overlay.bundles]) {
    if (row.state === "removed") removed += 1;
    else live += 1;
  }
  for (const passage of list(article.passages)) {
    const sealedStatus = passageStatus(passage);
    const merged: MergedLocation[] = [];
    const seen = new Set<string>();
    for (const loc of list(passage.locations)) {
      const key = typeof loc.pair_key === "string" ? loc.pair_key : null;
      const tag = key ? byKey.get(key) ?? null : null;
      if (key) seen.add(key);
      if (!tag) {
        merged.push({ loc, origin: "pipeline", state: "live", tag: null, author: null, baseRelation: null, baseNote: null, confirms: false });
        continue;
      }
      const confirms = tag.state === "live" && tag.relation === loc.relation && (tag.note ?? null) === (loc.note ?? null);
      const changed = tag.state === "live" && !confirms;
      merged.push({
        loc: changed ? { ...loc, relation: tag.relation, note: tag.note } : loc,
        origin: "pipeline",
        state: tag.state,
        tag,
        author: overlay.authors.get(tag.added_by) ?? null,
        baseRelation: changed ? loc.relation : null,
        baseNote: changed && (tag.note ?? null) !== (loc.note ?? null) ? loc.note : null,
        confirms,
      });
    }
    // Community tags at a pair the pipeline never located: their own cards.
    const passageKey = typeof passage.passage_key === "string" ? passage.passage_key : "";
    for (const tag of byPassageKey.get(passageKey) ?? []) {
      if (seen.has(tag.pair_key)) continue;
      // A community card has no commit of its own; show the universe's commit for
      // that library when the article's own locations name one, else none.
      const commit = list(passage.locations).find((loc) => loc.library === tag.library)?.commit ?? "";
      merged.push({
        loc: communityLocation(tag, commit),
        origin: "community",
        state: tag.state,
        tag,
        author: overlay.authors.get(tag.added_by) ?? null,
        baseRelation: null,
        baseNote: null,
        confirms: false,
      });
    }
    const liveLocations = merged.filter((m) => m.state === "live").map((m) => m.loc);
    // The passage's community coverage bundles (issue #173). A bundle counts
    // only when every part it names is still a live merged location whose
    // relation, after any community override, is hasPart: removing a part
    // withdraws it, as it withdraws a sealed claim, and so does relabelling one
    // away from hasPart, because it is then no longer a part of anything.
    const liveByPair = new Map<string, MergedLocation>();
    for (const m of merged) {
      const key = typeof m.loc.pair_key === "string" ? m.loc.pair_key : null;
      if (key && m.state === "live") liveByPair.set(key, m);
    }
    const bundles: MergedBundle[] = (bundlesByPassage.get(passageKey) ?? []).map((bundle) => {
      const parts = bundleParts(bundle);
      const locations = parts.map((key) => liveByPair.get(key) ?? null);
      const counts = bundle.state === "live" && parts.length >= MIN_BUNDLE_PARTS
        && parts.length <= MAX_BUNDLE_PARTS
        // The route refuses a repeated part; a row written another way (an
        // admin's SQL) must not count one location twice.
        && new Set(parts).size === parts.length
        && locations.every((m) => m !== null && m.loc.relation === BUNDLE_PART_RELATION);
      const status = counts
        ? bundleStandInStatus(String(passage.kind ?? ""),
          locations.map((m) => m!.loc), bundle.relation, statusRule)
        : null;
      return {
        bundle,
        parts,
        conjuncts: bundleConjuncts(bundle),
        locations,
        state: bundle.state,
        author: overlay.authors.get(bundle.added_by) ?? null,
        counts,
        status: status ?? null,
      };
    });
    const edited = merged.some((m) => m.tag !== null) || bundles.length > 0;
    // A passage nobody has touched keeps the status the store published,
    // byte for byte: the merge recomputes only where there is something to
    // merge. (The recomputation is a port of the builder's rule and a parity
    // test pins it, but "the sealed set is shown as the sealed set" is the
    // stronger promise, and it is cheaper to keep than to re-derive.)
    const status = edited
      ? mergedStatus(passage, liveLocations, statusRule, bundles.map((b) => b.status))
      : sealedStatus;
    byPassage.set(passage.id, { locations: merged, bundles, status, sealedStatus, edited });
    if (status !== "unresolved") byStatus[status] += 1;
  }
  const edits = overlay.tags.length + overlay.bundles.length;
  return {
    byPassage, live, removed, edits, byStatus,
    truncated: overlay.tags.length >= MAX_OVERLAY_ROWS || overlay.bundles.length >= MAX_OVERLAY_ROWS,
  };
}

// ---- trust tier and the daily caps (plan section 2.6) -----------------------

export type TrustTier = "new" | "autoconfirmed";

// Plan section 2.6, computed at write time rather than by a cron. R1 has no
// user_trust table (migration 0022, release R3) and no revert ratio (there are
// no reverts yet), so the tier is the two measurable halves of the rule:
// account age and overlay edit count. An agent takes its OWNER's tier and never
// exceeds it, which is the whole value of PR #126's rule that every agent names
// a person.
export const AUTOCONFIRMED_AGE_MS = 4 * 24 * 60 * 60 * 1000;
export const AUTOCONFIRMED_EDITS = 10;
export const DAILY_CAP: Record<TrustTier, number> = { new: 50, autoconfirmed: 500 };
export const DAY_MS = 24 * 60 * 60 * 1000;

export interface TagAllowance {
  tier: TrustTier;
  today: number;
  cap: number;
}

// One D1 round trip: the account's age, its overlay edits ever, and its writes
// in the last 24 hours. A write is a row the account most recently authored
// (added_by / updated_at) or removed (removed_by / removed_at) in the window, so
// changing a row created yesterday counts today.
//
// "The account" is the person AND every agent they minted (PR #126's
// agent_tokens.owner_id): an agent inherits its owner's tier and cap and never
// exceeds it, so minting agents buys no more daily volume than one person has.
export async function tagAllowance(
  env: Pick<Env, "DB">,
  accountId: string,
  now: number,
): Promise<TagAllowance> {
  const since = now - DAY_MS;
  // One tier and one cap over both overlay surfaces: a coverage bundle
  // (migration 0021) is an overlay edit like a tag, so minting bundles buys no
  // volume a tag would not. A deployment without 0021 falls back to the tags
  // alone rather than refusing every write. Each table is counted on its own
  // and the two are added: every count is then an indexed read on that table's
  // (added_by, updated_at) or (removed_by, removed_at) index, which a count
  // over a UNION of the two in a CTE is not — SQLite materialises a CTE read
  // twice and scans both tables, and this query runs on EVERY community write.
  // Plain ordered placeholders, as every other query in wiki/src uses.
  const mine = "(SELECT id FROM mine)";
  const everEdited = (table: string) => `(SELECT COUNT(*) FROM ${table} WHERE added_by IN ${mine})`;
  const editedToday = (table: string) => `(SELECT COUNT(*) FROM ${table}
              WHERE (added_by IN ${mine} AND updated_at >= ?)
                 OR (removed_by IN ${mine} AND removed_at >= ?))`;
  const counted = (coverage: boolean) => `WITH mine(id) AS (
       SELECT ? UNION SELECT agent_id FROM agent_tokens WHERE owner_id = ?
     )
     SELECT (SELECT created_at FROM users WHERE id = ?) AS created_at,
            ${everEdited("article_tags")}${coverage ? ` + ${everEdited("article_coverage")}` : ""} AS edits,
            ${editedToday("article_tags")}${coverage ? ` + ${editedToday("article_coverage")}` : ""} AS today`;
  type AllowanceRow = { created_at: number | null; edits: number; today: number };
  // The account three times, then the window's two bounds once per counted
  // table, in the order their clauses appear.
  const read = (coverage: boolean) => {
    const bounds = coverage ? [since, since, since, since] : [since, since];
    return env.DB.prepare(counted(coverage))
      .bind(accountId, accountId, accountId, ...bounds).all<AllowanceRow>();
  };
  let results: AllowanceRow[];
  try {
    ({ results } = await read(true));
  } catch (error) {
    if (!coverageTableMissing(error)) throw error;
    ({ results } = await read(false));
  }
  const row = results[0] ?? { created_at: null, edits: 0, today: 0 };
  // better-auth stores users.created_at in seconds (timestamp mode).
  const createdMs = typeof row.created_at === "number" ? row.created_at * 1000 : now;
  const old = now - createdMs >= AUTOCONFIRMED_AGE_MS;
  const tier: TrustTier = old && row.edits >= AUTOCONFIRMED_EDITS ? "autoconfirmed" : "new";
  return { tier, today: row.today, cap: DAILY_CAP[tier] };
}

// ---- rate-limit keys (plan section 2.6) -------------------------------------

// Burst budgets on TAG_LIMITER (60/min), keyed so a newcomer gets a tenth of an
// established editor's. An agent is NOT keyed here: all of one person's agents
// share PR #126's single 30/min key (agents.ts editLimitKey on EDIT_LIMITER), so
// minting five agents buys no more writes on this plane either.
export function tagLimitKey(user: AuthUser, tier: TrustTier): string {
  return tier === "new" ? `tagnew:${user.id}` : `tag:${user.id}`;
}

// The account whose tier, caps and agreement govern a write: an agent's owner,
// else the user themself.
export function accountFor(user: AuthUser): string {
  return user.role === "agent" && user.ownerId ? user.ownerId : user.id;
}

// ---- the CC0 agreement at first edit (decision 11) --------------------------

// The licence the contributed data is released under. Bump this id when the
// wording changes, and add the superseded id to CC0_ACCEPTED_VERSIONS unless
// the change is one a contributor must see and accept again.
export const CC0_AGREEMENT_VERSION = "cc0-1.1/2026-11";

export const CC0_AGREEMENT_TEXT =
  "I release the data I add here — passage tags (declaration names, relations and notes) " +
  "and Brain connections (their endpoints, kinds and evidence notes) — " +
  "into the public domain under CC0 1.0, and I confirm it is mine to release. " +
  "Wikipedia article text keeps its own licence (CC BY-SA 4.0) and Lean code keeps its library’s.";

// Acceptances that still stand for the surfaces `cc0-1.0/2026-11` described.
// Its words were "the mapping data I add here — the declaration names,
// relations and notes", which is exactly what the formalization reader's
// overlay tags and a pipeline submission store, so neither asks again.
export const CC0_ACCEPTED_VERSIONS: readonly string[] = ["cc0-1.0/2026-11", CC0_AGREEMENT_VERSION];

// Acceptances that stand for a BRAIN write. Only 1.1 names Brain connections
// and their evidence notes; 1.0 covered passage-tag mapping data and is not a
// grant for an edge's prose, so a contributor still on it is asked once more.
export const CC0_BRAIN_VERSIONS: readonly string[] = [CC0_AGREEMENT_VERSION];

export function agreementStands(
  agreement: { cc0_version: string } | null | undefined,
  versions: readonly string[] = CC0_ACCEPTED_VERSIONS,
): boolean {
  return !!agreement && versions.includes(agreement.cc0_version);
}

// What a writer is told when the grant is missing. The messages live here with
// the gate so the overlay and the Brain answer in the same words. The `UPDATE`
// pair is for a contributor who accepted an earlier wording that does not cover
// what they are writing now.
export const CC0_REQUIRED = "the CC0 agreement is required before your first edit";
export const CC0_UPDATE_REQUIRED =
  "the contribution agreement has been updated; read and accept it to make this edit";
export const TURNSTILE_REQUIRED = "a Turnstile check is required for a new account's first edit";
export const CC0_AGENT_REQUIRED =
  "the person who minted this agent must accept the CC0 contribution agreement at /agents first";
export const CC0_AGENT_UPDATE_REQUIRED =
  "the person who minted this agent must accept the updated CC0 contribution agreement at /agents first";

export interface UserAgreement {
  user_id: string;
  cc0_version: string;
  accepted_at: number;
  verified_at: number | null;
}

export async function loadAgreement(env: Pick<Env, "DB">, accountId: string): Promise<UserAgreement | null> {
  try {
    const { results } = await env.DB.prepare(
      "SELECT user_id, cc0_version, accepted_at, verified_at FROM user_agreements WHERE user_id = ?",
    ).bind(accountId).all<UserAgreement>();
    return results[0] ?? null;
  } catch (error) {
    if (!overlayTableMissing(error)) throw error;
    return null;
  }
}

// One acceptance row per person, recording the CURRENT version. A later
// re-acceptance (a wording change a surface needs granted explicitly) updates
// the version in place and KEEPS the original `accepted_at`: that column is the
// table's only timestamp and when the person first gave the grant is the fact
// worth holding, so the moment they took the update is not recorded. An
// acceptance never clears a Turnstile verification already on the row.
export function agreementAcceptStatement(
  env: Pick<Env, "DB">,
  accountId: string,
  now: number,
  verifiedAt: number | null,
): D1PreparedStatement {
  return env.DB.prepare(
    "INSERT INTO user_agreements (user_id, cc0_version, accepted_at, verified_at) VALUES (?, ?, ?, ?) " +
      "ON CONFLICT(user_id) DO UPDATE SET cc0_version = excluded.cc0_version, " +
      "accepted_at = user_agreements.accepted_at, " +
      "verified_at = COALESCE(user_agreements.verified_at, excluded.verified_at)",
  ).bind(accountId, CC0_AGREEMENT_VERSION, now, verifiedAt);
}

// Stamp a Turnstile pass onto an acceptance that was taken without one (a Brain
// form carries no widget, and a deployment with no secret skips the check).
export function agreementVerifyStatement(
  env: Pick<Env, "DB">,
  accountId: string,
  verifiedAt: number,
): D1PreparedStatement {
  return env.DB.prepare(
    "UPDATE user_agreements SET verified_at = ? WHERE user_id = ? AND verified_at IS NULL",
  ).bind(verifiedAt, accountId);
}

export function recordAgreement(
  env: Pick<Env, "DB">,
  accountId: string,
  now: number,
  verifiedAt: number | null,
): Promise<unknown> {
  return agreementAcceptStatement(env, accountId, now, verifiedAt).run();
}

// ---- Turnstile on a new account's first edit (plan section 2.6) -------------

export const TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
export const TURNSTILE_SCRIPT_ORIGIN = "https://challenges.cloudflare.com";
export const TURNSTILE_SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js";

export type TurnstileResult = "ok" | "skipped" | "failed";

// Verify a Turnstile token against Cloudflare. The SECRET is a Worker secret
// (docs/SECRETS.md TURNSTILE_SECRET) and never a value in this repository; the
// SITE key is public configuration (wrangler.jsonc vars TURNSTILE_SITE_KEY).
// Without the secret the check is SKIPPED and logged, so a deployment that has
// not been given the keys still takes edits rather than refusing everyone — the
// other controls (account required, the burst and daily caps, the brake) stand.
//
// The optional `remoteip` field is deliberately NOT sent: WikiLean does not keep
// IP addresses (migration 0013 scrubbed the last of them) and this check works
// without one.
export async function verifyTurnstile(
  env: Pick<Env, "TURNSTILE_SECRET">,
  token: string | null,
): Promise<TurnstileResult> {
  const secret = env.TURNSTILE_SECRET;
  if (!secret) {
    console.log(JSON.stringify({ event: "turnstile-skipped", reason: "no secret configured" }));
    return "skipped";
  }
  if (!token) return "failed";
  const form = new URLSearchParams({ secret, response: token });
  try {
    const res = await fetch(TURNSTILE_VERIFY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    if (!res.ok) return "failed";
    const body = (await res.json()) as { success?: boolean };
    return body?.success === true ? "ok" : "failed";
  } catch {
    return "failed";
  }
}

// ---- the shared first-edit gate --------------------------------------------
//
// Every write surface that stores a contributor's own text asks for the CC0
// grant once, at the first edit, and records it: the overlay's two tag routes
// (articles.ts) and the Brain's `POST /api/brain/edge` and
// `POST /api/brain/quickstatements` (brain-edits.ts), whose evidence notes are
// free text the site republishes as CC0.
//
// An agent cannot agree to a licence; its OWNER can, and must have, before any
// of that person's agents may write (plan section 5.3, "Licence").
//
// The step a caller must write once its own write goes ahead:
//   none    the row on file already covers this write
//   accept  record the current version (a first acceptance, or an update)
//   verify  the acceptance stands but was taken without a Turnstile check and
//           one has now been passed: stamp verified_at, leave the version be.
export type AgreementStep =
  | { record: "none" }
  | { record: "accept"; verifiedAt: number | null }
  | { record: "verify"; verifiedAt: number };

export interface AgreementWriter {
  // Whose acceptance governs this write: an agent's owner, else the person
  // themself (accountFor).
  account: string;
  // An agent's write, which is refused outright until its owner has accepted.
  isAgent: boolean;
  // Which acceptances cover what this surface stores: CC0_ACCEPTED_VERSIONS for
  // the overlay's passage tags, CC0_BRAIN_VERSIONS for a Brain connection,
  // whose notes only the 1.1 wording names.
  versions: readonly string[];
  // Ask for the Turnstile check that rides with an acceptance. The overlay's
  // form carries the widget and does; the Brain page has no widget, so its
  // forms record an unverified acceptance (verified_at stays NULL) rather than
  // refusing a contributor who has no way to pass the check. A surface that
  // asks also holds an UNVERIFIED acceptance to it — otherwise taking the
  // agreement on a widget-less form would buy a way past this check.
  turnstile: boolean;
}

function turnstileTokenOf(body: Record<string, unknown>): string | null {
  return typeof body.turnstile_token === "string" && body.turnstile_token !== "" ? body.turnstile_token : null;
}

export async function requireAgreement(
  c: Context<{ Bindings: Env }>,
  writer: AgreementWriter,
  body: Record<string, unknown>,
  now: number,
): Promise<AgreementStep | Response> {
  const agreement = await loadAgreement(c.env, writer.account);
  if (agreementStands(agreement, writer.versions)) {
    // The grant is on file. On a surface that asks for Turnstile, a PERSON's
    // acceptance recorded without one still owes the check: without this,
    // accepting on the Brain's widget-less form would skip the reader's
    // first-edit check. An agent is never asked — it cannot answer a challenge,
    // and it was never the challenge's subject (a bearer token with its own
    // caps is); its owner answers one at /agents or at their own first edit.
    if (!writer.turnstile || writer.isAgent || agreement!.verified_at !== null) return { record: "none" };
    const verdict = await verifyTurnstile(c.env, turnstileTokenOf(body));
    // No secret configured: the check is skipped site-wide, as it always was.
    if (verdict === "skipped") return { record: "none" };
    if (verdict === "failed") {
      return c.json({ ok: false, error: TURNSTILE_REQUIRED, turnstile_required: true }, 403);
    }
    return { record: "verify", verifiedAt: now };
  }
  // No grant this surface can use. An earlier version on file means the
  // contributor is being asked to accept an update, not a first agreement.
  const updating = agreement !== null;
  if (writer.isAgent) {
    return c.json({ ok: false, error: updating ? CC0_AGENT_UPDATE_REQUIRED : CC0_AGENT_REQUIRED }, 403);
  }
  if (body.cc0 !== true) {
    return c.json({
      ok: false, error: updating ? CC0_UPDATE_REQUIRED : CC0_REQUIRED, cc0_required: true,
      cc0_version: CC0_AGREEMENT_VERSION, cc0_text: CC0_AGREEMENT_TEXT,
    }, 403);
  }
  if (!writer.turnstile) return { record: "accept", verifiedAt: null };
  const verdict = await verifyTurnstile(c.env, turnstileTokenOf(body));
  if (verdict === "failed") {
    return c.json({ ok: false, error: TURNSTILE_REQUIRED, turnstile_required: true }, 403);
  }
  return { record: "accept", verifiedAt: verdict === "ok" ? now : null };
}

// The one write a step asks for, for a caller to put in its own batch (the
// overlay) or run on its own (the Brain). Null when there is nothing to record.
export function agreementStatement(
  env: Pick<Env, "DB">,
  accountId: string,
  now: number,
  step: AgreementStep,
): D1PreparedStatement | null {
  if (step.record === "accept") return agreementAcceptStatement(env, accountId, now, step.verifiedAt);
  if (step.record === "verify") return agreementVerifyStatement(env, accountId, step.verifiedAt);
  return null;
}
