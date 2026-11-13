// The articles plane's contract fixture for Worker tests: the golden store that
// scripts/articles/build_store.py builds from scripts/articles/fixtures/recordset
// (its Python test pins that the committed store equals a fresh build), served
// through a minimal R2-shaped bucket so the real ARTICLES code path runs.

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Env } from "../../src/env.js";

export const ARTICLES_STORE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../scripts/articles/fixtures/store");

export const WIDGET_PAGEID = 1001;
export const GADGET_PAGEID = 1003;
export const FIXTURE_CAMPAIGN = "fixture-2026-10";
export const FIXTURE_UNIVERSE_SHA = "0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

// Store path ("articles/v1/...") → file text.
export function articlesFixtureFiles(): Map<string, string> {
  const files = new Map<string, string>();
  for (const path of walk(ARTICLES_STORE_ROOT)) {
    files.set(relative(ARTICLES_STORE_ROOT, path).split("\\").join("/"), readFileSync(path, "utf8"));
  }
  return files;
}

export interface FakeBucket {
  get(key: string): Promise<{ text(): Promise<string> } | null>;
  gets: string[];
}

export function fakeBucket(files: Map<string, string>): FakeBucket {
  const gets: string[] = [];
  return {
    gets,
    async get(key: string) {
      gets.push(key);
      const value = files.get(key);
      return value === undefined ? null : { text: async () => value };
    },
  };
}

// Binds ARTICLES on a harness env; returns the bucket so tests can count reads.
export function bindArticles(env: Env, files: Map<string, string> = articlesFixtureFiles()): FakeBucket {
  const bucket = fakeBucket(files);
  (env as unknown as { ARTICLES: FakeBucket }).ARTICLES = bucket;
  return bucket;
}

export function fixtureArticle(pageid = WIDGET_PAGEID): Record<string, any> {
  return JSON.parse(articlesFixtureFiles().get(`articles/v1/articles/${pageid}.json`)!);
}

// ---- the /2 store the community overlay needs ----------------------------------------------
//
// The committed fixture is a cf-locate-recordset/1 store: it carries neither
// passage_key nor pair_key, and the tag routes refuse it by design (a /1 set
// cannot take an overlay — docs/ARTICLES.md, "Community tags"). These helpers
// derive a /2 shape from it in the test process alone, so the Python builder's
// golden fixture is untouched: every passage gets a passage_key and every
// location the pair_key the server recomputes from it, by the producer's own
// formulas (scripts/articles/build_store.py passage_key / pair_key).

const sha256 = (s: string) => createHash("sha256").update(s, "utf8").digest("hex");

// passage_key = sha256("<pageid>\n<span_sha256>"). The fixture records no span
// hash in its view model, so the test stands one in, deterministically.
export function passageKeyFor(pageid: number, passageId: string): string {
  return sha256(`${pageid}\n${sha256(passageId)}`);
}

// pair_key = sha256("<passage_key>\n<repository>\n<declaration>"), the
// repository being the Lean end's URI — NOT the library id.
export function pairKeyFor(passageKey: string, repository: string, declaration: string): string {
  return sha256(`${passageKey}\n${repository}\n${declaration}`);
}

export const MATHLIB_REPO = "https://github.com/leanprover-community/mathlib4";

// The golden store with the /2 keys and a /2 seal. `status_rule` 2 leaves every
// location's own status unchanged (only rule 3, R-BROAD, changes one), so the
// published statuses still hold.
export function articlesPairedFiles(files: Map<string, string> = articlesFixtureFiles()): Map<string, string> {
  const out = new Map(files);
  for (const pageid of [WIDGET_PAGEID, GADGET_PAGEID]) {
    const key = `articles/v1/articles/${pageid}.json`;
    const article = JSON.parse(out.get(key)!);
    article.seal = {
      set_id: "fixture-set",
      schema: "cf-locate-recordset/2",
      status_rule: 2,
      created: "2026-10-17T12:00:00Z",
      skill_commit: "3bceabcee0724ffdf50ce65dde3ab10048c72cba",
    };
    for (const passage of article.passages) {
      passage.passage_key = passageKeyFor(pageid, passage.id);
      for (const loc of passage.locations) {
        loc.pair_key = pairKeyFor(passage.passage_key, loc.repo, loc.declaration);
      }
    }
    out.set(key, JSON.stringify(article));
  }
  return out;
}

// One passage of the /2 fixture, by page id and index in the view model.
export function pairedPassage(pageid: number, index: number): Record<string, any> {
  const files = articlesPairedFiles();
  const article = JSON.parse(files.get(`articles/v1/articles/${pageid}.json`)!);
  return article.passages[index];
}

// ---- the passage a community coverage bundle is made on (issue #173) -----------------------
//
// The /2 fixture with the Gadget's covered theorem (page 1003, passage 1) stripped of its
// SEALED claim: two proved hasPart locations and nothing saying they together state the
// passage, which is exactly the shape the issue is about — the published status says
// formalized because of the claim, and without one the parts alone leave it partial. A
// community bundle over those two parts must put it back, so the fixture's status is set to
// what the parts alone give. Derived in the test process; the committed store is untouched.
export const COVERED_PAGEID = GADGET_PAGEID;
export const COVERED_INDEX = 1;

export function articlesBundleFiles(files: Map<string, string> = articlesPairedFiles()): Map<string, string> {
  const out = new Map(files);
  const key = `articles/v1/articles/${COVERED_PAGEID}.json`;
  const article = JSON.parse(out.get(key)!);
  const passage = article.passages[COVERED_INDEX];
  delete passage.coverage;
  passage.status = "partial";
  passage.basis = null;
  // The article's own counts say the same thing, as a built store's would.
  article.summary.by_status.formalized -= 1;
  article.summary.by_status.partial += 1;
  out.set(key, JSON.stringify(article));
  return out;
}

// That passage, as the bundle fixture's store serves it.
export function bundlePassage(): Record<string, any> {
  const article = JSON.parse(articlesBundleFiles().get(`articles/v1/articles/${COVERED_PAGEID}.json`)!);
  return article.passages[COVERED_INDEX];
}

// ---- a statement-only article (phase 1) -----------------------------------------------------
//
// The store side of phase 1 is P3's; this is the contract the Worker reads, written out by
// hand from the design's section 3 so the two can be built in parallel: passages with
// outcome "unchecked" and no status, a fifth summary status not_checked, per-article
// `checked: false`, a statement set's seal, and the golden store's universe attached as the
// TAGGING universe (the libraries a community tag may name — nothing has searched them).
// Every word of the article is invented; no Wikipedia text is in this repository.

export const SPROCKET_PAGEID = 1005;
export const SPROCKET_TITLE = "Sprocket theorem";
export const SPROCKET_REVISION = 3003;
export const STATEMENT_CAMPAIGN = "run:cut-fixture-2026-11";
export const STATEMENT_SET_SCHEMA = "wikilean.statement-set/1";
export const STATEMENT_SEAL_SHA = "a".repeat(64);
// Its three statements, in the store's order; the first is the article's subject.
export const SPROCKET_HEXES = ["a1b2c3d4", "b2c3d4e5", "c3d4e5f6"];

const SPROCKET_PASSAGES = [
  {
    hex: SPROCKET_HEXES[0], kind: "theorem", prominence: "subject", section: ["Statement"],
    quote: { exact: "every sprocket admits a balanced cog", prefix: "The sprocket theorem states that ", suffix: " of finite order." },
    statement: "Every sprocket admits a balanced cog of finite order.", conjuncts: null,
  },
  {
    hex: SPROCKET_HEXES[1], kind: "definition", prominence: "central", section: ["Definitions"],
    quote: { exact: "a sprocket is balanced if every cog has equal weight", prefix: "Following Jones, ", suffix: "." },
    statement: "A sprocket is balanced when every one of its cogs has equal weight.", conjuncts: null,
  },
  {
    hex: SPROCKET_HEXES[2], kind: "theorem", prominence: "supporting", section: ["Properties"],
    quote: { exact: "every balanced sprocket is finite and has a unique cog", prefix: "It follows that ", suffix: "." },
    statement: "Every balanced sprocket is finite, and it has exactly one cog.",
    conjuncts: [{ id: "c1", text: "Every balanced sprocket is finite." }, { id: "c2", text: "A balanced sprocket has exactly one cog." }],
  },
];

function sprocketArticle(): Record<string, any> {
  const sourceId = `enwiki-${SPROCKET_PAGEID}-${SPROCKET_REVISION}`;
  const passages = SPROCKET_PASSAGES.map((p) => {
    const id = `${sourceId}::In-text #${p.hex}`;
    return {
      id,
      item: `In-text #${p.hex}`,
      passage_key: passageKeyFor(SPROCKET_PAGEID, id),
      kind: p.kind,
      section: p.section,
      prominence: p.prominence,
      quote: p.quote,
      quote_capped: false,
      passage_ends_in_suffix: false,
      subject_uri: `https://en.wikipedia.org/w/index.php?title=Sprocket_theorem&oldid=${SPROCKET_REVISION}`,
      // The phase-1 shape: the cut ran, no locate run has.
      outcome: "unchecked",
      status: null,
      basis: null,
      coverage: null,
      statement: p.statement,
      conjuncts: p.conjuncts,
      locations: [],
      absence: null,
      unresolved: null,
    };
  });
  const byStatus = { formalized: 0, statement_only: 0, partial: 0, not_formalized: 0, not_checked: 0, unresolved: 0 };
  const prominence: Record<string, any> = {};
  for (const passage of passages) {
    byStatus.not_checked += 1;
    const bucket = prominence[passage.prominence] ?? (prominence[passage.prominence] = {
      passages: 0, by_status: { formalized: 0, statement_only: 0, partial: 0, not_formalized: 0, not_checked: 0, unresolved: 0 },
    });
    bucket.passages += 1;
    bucket.by_status.not_checked += 1;
  }
  return {
    schema: "wikilean.articles.article/1",
    pageid: SPROCKET_PAGEID,
    title: SPROCKET_TITLE,
    revision: SPROCKET_REVISION,
    source_id: sourceId,
    source: { html_sha256: "b".repeat(64), layer_sha256: "c".repeat(64), renderer: "web-html/3" },
    wikidata: null,
    assessment: { class: "C", importance: "mid" },
    campaign: STATEMENT_CAMPAIGN,
    // The tagging universe: the same libraries the golden store's articles name, which is
    // what makes POST /api/articles/tag work on a statement-only article.
    universe: { id: "u-fixture", sha256: FIXTURE_UNIVERSE_SHA, frozen: "2026-10-17", libraries: 5 },
    checked: false,
    record_set: STATEMENT_SEAL_SHA,
    records_sha256: null,
    licence: { text: "CC-BY-SA-4.0", mapping: "CC0-1.0" },
    seal: {
      set_id: "fixture-statements",
      schema: STATEMENT_SET_SCHEMA,
      created: "2026-11-12T09:00:00Z",
      producer_commit: "7c1d9a2e5b4f6083a1cc27d4e9f0b3a85d6172ef",
    },
    summary: {
      passages: passages.length,
      locations: 0,
      related: 0,
      by_status: byStatus,
      by_prominence: prominence,
      by_library: {},
      by_proof_status: {},
      by_relation: {},
    },
    passages,
  };
}

// The index row of one article, in the shard's own field order. not_checked is written LAST,
// after `libraries`: the Worker reads a shard's columns by name, so where the builder puts
// the new one does not matter.
const STATEMENT_INDEX_FIELDS = ["title", "pageid", "revision", "passages", "formalized", "statement_only",
  "partial", "not_formalized", "unresolved", "importance", "libraries", "not_checked"];

function indexRow(article: Record<string, any>): unknown[] {
  const by = article.summary.by_status;
  return [article.title, article.pageid, article.revision, article.summary.passages, by.formalized,
    by.statement_only, by.partial, by.not_formalized, by.unresolved,
    article.assessment?.importance ?? null, [], by.not_checked];
}

// The shard a title falls in (build_store INDEX_HEX = 1).
function shardOf(title: string): string {
  return sha256(title.normalize("NFC").replace(/[\s_]+/g, " ").trim()).slice(0, 1);
}

// A store of one statement-only article: its view, its index shard, the golden store's
// universe object, and a current.json whose counts are all not_checked.
export function articlesStatementFiles(): Map<string, string> {
  const golden = articlesFixtureFiles();
  const article = sprocketArticle();
  const shard = shardOf(SPROCKET_TITLE);
  const files = new Map<string, string>();
  files.set(`articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`, golden.get(`articles/v1/universes/${FIXTURE_UNIVERSE_SHA}.json`)!);
  files.set(`articles/v1/articles/${SPROCKET_PAGEID}.json`, JSON.stringify(article));
  files.set(`articles/v1/index/${shard}.json`, JSON.stringify({
    schema: "wikilean.articles.index/1", shard, fields: STATEMENT_INDEX_FIELDS, articles: [indexRow(article)],
  }));
  files.set("articles/v1/current.json", JSON.stringify({
    schema: "wikilean.articles.current/1",
    campaign: STATEMENT_CAMPAIGN,
    built_at: "2026-11-12T09:00:00Z",
    universe: article.universe,
    record_set: {
      seal_sha256: STATEMENT_SEAL_SHA, set_id: "fixture-statements", created: "2026-11-12T09:00:00Z",
      producer_commit: article.seal.producer_commit, schema: STATEMENT_SET_SCHEMA, byte_budget: 0,
    },
    counts: {
      articles: 1,
      passages: article.summary.passages,
      locations: 0,
      related: 0,
      by_status: article.summary.by_status,
      by_prominence: article.summary.by_prominence,
      by_library: {},
      by_importance: { mid: { articles: 1, passages: article.summary.passages, by_status: article.summary.by_status } },
    },
    index: { hex: 1, shards: [shard], entries: 1 },
  }));
  return files;
}

// The same article beside the golden store's located ones: one build, both kinds, which is
// what the combine rule allows (the statement-set design, section 3).
export function articlesMixedFiles(): Map<string, string> {
  const files = articlesPairedFiles();
  const statements = articlesStatementFiles();
  const article = JSON.parse(statements.get(`articles/v1/articles/${SPROCKET_PAGEID}.json`)!);
  files.set(`articles/v1/articles/${SPROCKET_PAGEID}.json`, statements.get(`articles/v1/articles/${SPROCKET_PAGEID}.json`)!);
  const shard = shardOf(SPROCKET_TITLE);
  const key = `articles/v1/index/${shard}.json`;
  const existing = files.has(key)
    ? JSON.parse(files.get(key)!)
    : { schema: "wikilean.articles.index/1", shard, fields: STATEMENT_INDEX_FIELDS, articles: [] };
  // The shard the golden store wrote has no not_checked column; the combined build's does.
  const fields: string[] = existing.fields.includes("not_checked") ? existing.fields : [...existing.fields, "not_checked"];
  const rows = existing.articles.map((row: unknown[]) => (row.length === fields.length ? row : [...row, 0]));
  files.set(key, JSON.stringify({ ...existing, fields, articles: [...rows, indexRow(article)] }));
  const current = JSON.parse(files.get("articles/v1/current.json")!);
  current.counts.articles += 1;
  current.counts.passages += article.summary.passages;
  current.counts.by_status.not_checked = article.summary.by_status.not_checked;
  for (const [name, bucket] of Object.entries(article.summary.by_prominence as Record<string, any>)) {
    const into = current.counts.by_prominence[name] ??= { passages: 0, by_status: {} };
    into.passages += bucket.passages;
    into.by_status.not_checked = (into.by_status.not_checked ?? 0) + bucket.by_status.not_checked;
  }
  const mid = current.counts.by_importance.mid ??= { articles: 0, passages: 0, by_status: {} };
  mid.articles += 1;
  mid.passages += article.summary.passages;
  mid.by_status.not_checked = (mid.by_status.not_checked ?? 0) + article.summary.by_status.not_checked;
  current.index.entries += 1;
  if (!current.index.shards.includes(shard)) current.index.shards = [...current.index.shards, shard].sort();
  files.set("articles/v1/current.json", JSON.stringify(current));
  return files;
}

// The statement-only article's marked revision: every mark data-status="unchecked", the
// passage's own outcome, as the design's section 3 writes it.
export function statementReaderHtml(): string {
  const mark = (hex: string, body: string) =>
    `<mark class="atl" data-p="${hex}" data-status="unchecked" id="p-${hex}">${body}</mark>`;
  return `<div class="mw-parser-output">
<p>In mechanics, the <b>sprocket theorem</b> states that ${mark(SPROCKET_HEXES[0], "every sprocket admits a balanced cog")} of finite order.</p>
<h2 id="Definitions">Definitions</h2>
<p>Following Jones, ${mark(SPROCKET_HEXES[1], "a sprocket is balanced if every cog has equal weight")}.</p>
<h2 id="Properties">Properties</h2>
<p>It follows that ${mark(SPROCKET_HEXES[2], "every balanced sprocket is finite and has a unique cog")}.</p>
</div>`;
}

// That store with the marked revision, as build_store --reader-html would publish it.
export function articlesStatementReaderFiles(): Map<string, string> {
  const files = articlesStatementFiles();
  const html = statementReaderHtml();
  const article = JSON.parse(files.get(`articles/v1/articles/${SPROCKET_PAGEID}.json`)!);
  article.reader = {
    sha256: createHash("sha256").update(html, "utf8").digest("hex"),
    bytes: Buffer.byteLength(html, "utf8"),
    marks: (html.match(/<mark class="atl"/g) ?? []).length,
    passages: article.passages.length,
    unplaced: [],
  };
  files.set(`articles/v1/articles/${SPROCKET_PAGEID}.json`, JSON.stringify(article));
  files.set(`articles/v1/reader/${SPROCKET_PAGEID}.html`, html);
  return files;
}

// ---- the article reader (build_store --reader-html) ----------------------------------------
//
// A synthetic marked revision of the Widget lemma fixture (page 1001), in the shape the builder
// stores as reader/<pageid>.html: MediaWiki-like parse HTML, each passage's text wrapped in
// <mark class="atl" data-p="<item hex>" data-status="<status>">, the first mark of each passage
// with id="p-<hex>". The passages read in another order than the store lists them (the
// definition comes first), and the bound's text runs through a link, so it is three marks. The
// conjecture has no status (it is unresolved in the store), so it is never marked: the builder
// refuses a mark on it, and the view lists it as unplaced.
// Every value is made up; the builder's own tests check the real contract.

export const READER_ORDER = ["3fa91c07", "91aa02de", "5b0e44a1"];
// The passage with no status, which the pages set aside.
export const NO_STATUS_HEX = "0c4d77b1";

const mark = (hex: string, status: string, text: string, first = true) =>
  `<mark class="atl" data-p="${hex}" data-status="${status}"${first ? ` id="p-${hex}"` : ""}>${text}</mark>`;

// Two formulas outside the passages, as Wikipedia's MathML writes them: a double-struck R (which
// MathML Core draws as a plain R) and an operator name with its function application.
const formula = (tex: string, mathml: string) =>
  `<span class="mwe-math-element mwe-math-element-inline"><span class="mwe-math-mathml-inline mwe-math-mathml-a11y">` +
  `<math xmlns="http://www.w3.org/1998/Math/MathML" alttext="{\\displaystyle ${tex}}"><semantics><mrow class="MJX-TeXAtom-ORD">` +
  `<mstyle displaystyle="true" scriptlevel="0">${mathml}</mstyle></mrow>` +
  `<annotation encoding="application/x-tex">{\\displaystyle ${tex}}</annotation></semantics></math></span></span>`;
const MATH_R = formula("\\mathbb {R} ", `<mrow class="MJX-TeXAtom-ORD"><mi mathvariant="double-struck">R</mi></mrow>`);
const MATH_LOG = formula("\\log n", `<mi>log</mi><mo>&#x2061;</mo><mi>n</mi>`);

export function readerHtml(omit: string[] = []): string {
  const m = (hex: string, status: string, text: string, first = true) => (omit.includes(hex) ? text : mark(hex, status, text, first));
  return `<div class="mw-parser-output"><style data-mw-deduplicate="TemplateStyles:r1">.mw-parser-output .hatnote{font-style:italic;padding-left:1.6em}</style>
<div role="note" class="hatnote navigation-not-searchable">This article is about widgets. For gadgets, see <a href="/articles/1003/Gadget_(mathematics)">Gadget (mathematics)</a>.</div>
<p>In combinatorics, the <b>widget lemma</b> states that ${m("3fa91c07", "formalized", "every finite widget admits a spanning gadget")} of minimal degree.<sup class="reference" id="cite_ref-1"><a href="#cite_note-1">[1]</a></sup></p>
<h2 id="Definitions">Definitions</h2>
<p>Following Smith, ${m("91aa02de", "not_formalized", "a widget is balanced if every gadget has equal weight")}.</p>
<p>A widget over ${MATH_R} has at most ${MATH_LOG} gadgets.</p>
<h2 id="Statement">Statement</h2>
<p>${m("5b0e44a1", "statement_only", "Moreover, the number of ")}<a href="https://en.wikipedia.org/wiki/Spanning_tree" target="_blank" rel="noopener noreferrer">${m("5b0e44a1", "statement_only", "spanning gadgets", false)}</a>${m("5b0e44a1", "statement_only", " of a widget is at most its rank.", false)}</p>
<h2 id="Open_problems">Open problems</h2>
<p>It is conjectured that every balanced widget is the image of a regular one.</p>
<h2 id="References">References</h2>
<ol class="references"><li id="cite_note-1"><span class="reference-text">A. Smith, <i>Widgets</i> (2020).</span></li></ol>
</div>`;
}

// The golden store with the Widget lemma's marked revision: reader/1001.html and the view's
// `reader` (its sha256, as the page checks). `omit` leaves passages unmarked (unplaced);
// `html` stores other bytes than the view names (a store written in part).
export function articlesReaderFiles(opts: { omit?: string[]; html?: string } = {}): Map<string, string> {
  const files = articlesFixtureFiles();
  const html = readerHtml(opts.omit ?? []);
  const article = fixtureArticle(WIDGET_PAGEID);
  const unplaced = article.passages
    .filter((p: { item: string }) => [...(opts.omit ?? []), NO_STATUS_HEX].some((hex) => p.item.endsWith(`#${hex}`)))
    .map((p: { id: string }) => p.id);
  article.reader = {
    sha256: createHash("sha256").update(html, "utf8").digest("hex"),
    bytes: Buffer.byteLength(html, "utf8"),
    marks: (html.match(/<mark class="atl"/g) ?? []).length,
    passages: article.passages.length - unplaced.length,
    unplaced,
  };
  files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
  files.set(`articles/v1/reader/${WIDGET_PAGEID}.html`, opts.html ?? html);
  return files;
}

// ---- the store the Python builder really writes (phase 1's two halves, joined) ---------------
//
// Every fixture above is written by hand in this file; this one is the committed golden store
// of a MIXED build — the record set beside the pipeline's synthetic sealed cut, with the cut's
// first article's marked revision — which scripts/articles/build_store.py wrote and
// scripts/articles/test_build_store.py (StatementSetTests, on the statements/store branch)
// rebuilds byte for byte from its inputs:
//
//   python3 scripts/articles/build_store.py --record-set scripts/articles/fixtures/recordset \
//       --statement-set scripts/pipeline/fixtures/statementset \
//       --tagging-universe scripts/articles/fixtures/recordset/universe.json \
//       --reader-html scripts/articles/fixtures/reader-html-statements.json \
//       --campaign mixed-fixture --out scripts/articles/fixtures/store-mixed --replace
//
// So the builder and the Worker cannot drift: test/articles-store-mixed.test.ts serves these
// bytes, and the builder's own test says they are the bytes it writes.
export const BUILT_STORE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../scripts/articles/fixtures/store-mixed");

// The cut's two articles and the record set's two, as the builder names them.
export const BUILT_CUT_PAGEID = 1005;
export const BUILT_CUT_TITLE = "Sprocket bundle";
export const BUILT_CUT_PATH = "/articles/1005/Sprocket_bundle";
export const BUILT_CUT_CAMPAIGN = "fixture-2026-11";
export const BUILT_LOCATED_PAGEID = WIDGET_PAGEID;

export function articlesBuiltMixedFiles(): Map<string, string> {
  const files = new Map<string, string>();
  for (const path of walk(BUILT_STORE_ROOT)) {
    files.set(relative(BUILT_STORE_ROOT, path).split("\\").join("/"), readFileSync(path, "utf8"));
  }
  return files;
}
